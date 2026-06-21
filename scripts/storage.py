import csv
import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
LEGACY_QUEUE_FIELDS = ["type", "externalId", "title", "status", "source", "addedAt", "error"]
QUEUE_FIELDS = [
    *LEGACY_QUEUE_FIELDS,
    "tmdbId",
    "radarrMovieId",
    "retryCount",
    "nextRetryAt",
    "stateChangedAt",
    "lastCheckedAt",
]


class SQLiteStore:
    """Transactional state store with JSON/CSV rollback exports."""

    def __init__(self, data_dir, default_config_factory):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "driparr.db"
        self.config_path = self.data_dir / "config.json"
        self.queue_path = self.data_dir / "queue.csv"
        self.default_config_factory = default_config_factory
        self._lock = threading.RLock()
        self._initialized = False

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self._lock:
            if self._initialized:
                return
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS config (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS queue (
                        position INTEGER PRIMARY KEY,
                        media_type TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT '',
                        added_at TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        tmdb_id TEXT NOT NULL DEFAULT '',
                        radarr_movie_id TEXT NOT NULL DEFAULT '',
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        next_retry_at TEXT NOT NULL DEFAULT '',
                        state_changed_at TEXT NOT NULL DEFAULT '',
                        last_checked_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS queue_status_position_idx
                        ON queue(status, position);
                    CREATE INDEX IF NOT EXISTS queue_identity_source_idx
                        ON queue(media_type, external_id, source);
                    """
                )
                imported = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'legacy_imported'"
                ).fetchone()
                if not imported:
                    self._backup_legacy_files()
                    self._import_legacy(connection)
                    connection.execute(
                        "INSERT OR REPLACE INTO metadata(key, value) VALUES('legacy_imported', 'true')"
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            self._initialized = True
            self._export_compatibility_files()

    def _backup_legacy_files(self):
        existing = [path for path in (self.config_path, self.queue_path) if path.exists()]
        if not existing:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = self.data_dir / "backups" / f"pre-sqlite-{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for path in existing:
            shutil.copy2(path, backup_dir / path.name)

    def _import_legacy(self, connection):
        config = self.default_config_factory()
        if self.config_path.exists():
            with self.config_path.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
        connection.execute(
            "INSERT OR REPLACE INTO config(id, payload, updated_at) VALUES(1, ?, ?)",
            (json.dumps(config, ensure_ascii=False), self._now()),
        )

        rows = []
        if self.queue_path.exists():
            with self.queue_path.open("r", newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
        self._replace_queue(connection, rows)

    def read_config(self):
        self.initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT payload FROM config WHERE id = 1").fetchone()
            if not row:
                return self.default_config_factory()
            return json.loads(row["payload"])

    def save_config(self, config):
        self.initialize()
        payload = json.dumps(config, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO config(id, payload, updated_at) VALUES(1, ?, ?)",
                (payload, self._now()),
            )
            connection.commit()
        self._atomic_write_text(self.config_path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")

    def read_queue(self):
        self.initialize()
        with self._lock, self._connect() as connection:
            records = connection.execute("SELECT * FROM queue ORDER BY position").fetchall()
        return [self._db_to_queue(row) for row in records]

    def write_queue(self, rows):
        self.initialize()
        normalized = [self._normalize_queue_row(row) for row in rows]
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_queue(connection, normalized)
            connection.commit()
        self._export_queue(normalized)

    def health(self):
        self.initialize()
        with self._lock, self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
            schema = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            queue_count = connection.execute("SELECT COUNT(*) AS count FROM queue").fetchone()["count"]
        return {
            "ok": True,
            "database": str(self.db_path),
            "schemaVersion": int(schema["value"]) if schema else 0,
            "queueCount": int(queue_count),
        }

    def backup_if_due(self, interval_hours=24, retention=14):
        self.initialize()
        backup_dir = self.data_dir / "backups" / "database"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backups = sorted(backup_dir.glob("driparr-*.db"), key=lambda path: path.stat().st_mtime)
        if backups and time.time() - backups[-1].stat().st_mtime < max(1, interval_hours) * 3600:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = backup_dir / f"driparr-{stamp}.db"
        with self._lock:
            source = sqlite3.connect(self.db_path, timeout=30)
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
                source.close()
        backups = sorted(backup_dir.glob("driparr-*.db"), key=lambda path: path.stat().st_mtime)
        for expired in backups[:-max(1, int(retention))]:
            expired.unlink(missing_ok=True)
        return destination

    def _replace_queue(self, connection, rows):
        connection.execute("DELETE FROM queue")
        for position, raw in enumerate(rows):
            row = self._normalize_queue_row(raw)
            connection.execute(
                """
                INSERT INTO queue(
                    position, media_type, external_id, title, status, source, added_at,
                    error, tmdb_id, radarr_movie_id, retry_count, next_retry_at,
                    state_changed_at, last_checked_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position,
                    row["type"],
                    row["externalId"],
                    row["title"],
                    row["status"],
                    row["source"],
                    row["addedAt"],
                    row["error"],
                    row["tmdbId"],
                    row["radarrMovieId"],
                    int(row["retryCount"] or 0),
                    row["nextRetryAt"],
                    row["stateChangedAt"],
                    row["lastCheckedAt"],
                ),
            )

    def _db_to_queue(self, row):
        return {
            "type": row["media_type"],
            "externalId": row["external_id"],
            "title": row["title"],
            "status": row["status"],
            "source": row["source"],
            "addedAt": row["added_at"],
            "error": row["error"],
            "tmdbId": row["tmdb_id"],
            "radarrMovieId": row["radarr_movie_id"],
            "retryCount": str(row["retry_count"]),
            "nextRetryAt": row["next_retry_at"],
            "stateChangedAt": row["state_changed_at"],
            "lastCheckedAt": row["last_checked_at"],
        }

    @staticmethod
    def _normalize_queue_row(row):
        return {field: str((row or {}).get(field, "") or "") for field in QUEUE_FIELDS}

    def _export_compatibility_files(self):
        config = self.read_config()
        rows = self.read_queue()
        self._atomic_write_text(self.config_path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        self._export_queue(rows)

    def _export_queue(self, rows):
        temporary = self.queue_path.with_name(f".{self.queue_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=LEGACY_QUEUE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.queue_path)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _atomic_write_text(path, text):
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
