import csv
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ASSETS = ROOT / "assets"
CONFIG_PATH = DATA / "config.json"
QUEUE_PATH = DATA / "queue.csv"
QUEUE_FIELDS = ["type", "externalId", "title", "status", "source", "addedAt", "error"]
LOCK = threading.Lock()
LAST_RUN = {"at": None, "message": "Worker has not started yet."}
LAST_EVENTS = []
SESSIONS = {}
SESSION_TTL_SECONDS = 60 * 60 * 12
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_CSV_CHARS = 2 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def push_event(level, title, detail=""):
    LAST_EVENTS.insert(
        0,
        {
            "at": utc_now(),
            "level": str(level or "info"),
            "title": str(title or ""),
            "detail": str(detail or ""),
        },
    )
    del LAST_EVENTS[80:]


def safe_int(value, default, minimum=None, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def format_time_only(value):
    dt = parse_iso_datetime(value)
    if not dt:
        return ""
    return dt.astimezone().strftime("%H:%M")


def format_eta_from_iso(value):
    dt = parse_iso_datetime(value)
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    seconds = int((dt.astimezone(timezone.utc) - now).total_seconds())
    if seconds <= 0:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes = max(1, remainder // 60)
    if hours:
        return f"ETA {hours}h {minutes}m"
    return f"ETA {minutes}m"


def env_admin_username():
    return os.getenv("DRIPARR_ADMIN_USERNAME", "admin")


def env_admin_password():
    return os.getenv("DRIPARR_ADMIN_PASSWORD", "admin")


def env_session_secret():
    return os.getenv("DRIPARR_SESSION_SECRET", "replace-me")


def env_force_language():
    value = os.getenv("DRIPARR_FORCE_LANGUAGE", "").strip().lower()
    return value if value in ("en", "nl", "de") else ""


def env_sync_poll_seconds():
    try:
        return max(10, int(os.getenv("DRIPARR_SYNC_POLL_SECONDS", "60") or "60"))
    except ValueError:
        return 60


def hash_password(password, salt):
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150000)
    return derived.hex()


def app_account_configured(config):
    app_cfg = config.get("app", {})
    return bool(app_cfg.get("adminUsername") and app_cfg.get("adminPasswordHash") and app_cfg.get("adminPasswordSalt"))


def verify_login(config, username, password):
    app_cfg = config.get("app", {})
    if app_account_configured(config):
        expected_user = str(app_cfg.get("adminUsername", ""))
        salt = str(app_cfg.get("adminPasswordSalt", ""))
        expected_hash = str(app_cfg.get("adminPasswordHash", ""))
        candidate_hash = hash_password(password, salt)
        return hmac.compare_digest(username, expected_user) and hmac.compare_digest(candidate_hash, expected_hash)
    return hmac.compare_digest(username, env_admin_username()) and hmac.compare_digest(password, env_admin_password())


def ensure_data():
    DATA.mkdir(exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(default_config(), indent=2), encoding="utf-8")
    if not QUEUE_PATH.exists():
        write_queue([])


def default_config():
    return {
        "app": {
            "setupComplete": False,
            "intervalMinutes": 60,
            "maxItemsPerRun": 1,
            "workerEnabled": False,
            "dripMode": "sync",
            "tmdbImporterEnabled": False,
            "imdbImporterEnabled": True,
            "onboardingDismissed": False,
            "notifyEnabled": False,
            "notifyWebhookUrl": "",
            "runHistory": [],
            "adminUsername": "",
            "adminPasswordHash": "",
            "adminPasswordSalt": "",
        },
        "radarr": {
            "enabled": True,
            "url": "",
            "apiKey": "",
            "qualityProfileId": 1,
            "rootFolderPath": "/movies",
            "minimumAvailability": "released",
            "searchOnAdd": True,
        },
        "sonarr": {
            "enabled": False,
            "url": "",
            "apiKey": "",
            "qualityProfileId": 1,
            "rootFolderPath": "/tv",
            "languageProfileId": 1,
            "seasonFolder": True,
            "searchOnAdd": True,
        },
        "lists": [],
    }


def read_config():
    ensure_data()
    with CONFIG_PATH.open("r", encoding="utf-8-sig") as handle:
        config = json.load(handle)

    app_cfg = config.setdefault("app", {})
    if "tmdbImporterEnabled" not in app_cfg:
        app_cfg["tmdbImporterEnabled"] = False
    if "imdbImporterEnabled" not in app_cfg:
        app_cfg["imdbImporterEnabled"] = True
    if app_cfg.get("dripMode") not in ("timed", "sync"):
        app_cfg["dripMode"] = "sync"
    if "onboardingDismissed" not in app_cfg:
        app_cfg["onboardingDismissed"] = False
    if "notifyEnabled" not in app_cfg:
        app_cfg["notifyEnabled"] = False
    if "notifyWebhookUrl" not in app_cfg:
        app_cfg["notifyWebhookUrl"] = ""
    if "runHistory" not in app_cfg or not isinstance(app_cfg.get("runHistory"), list):
        app_cfg["runHistory"] = []
    if "adminUsername" not in app_cfg:
        app_cfg["adminUsername"] = ""
    if "adminPasswordHash" not in app_cfg:
        app_cfg["adminPasswordHash"] = ""
    if "adminPasswordSalt" not in app_cfg:
        app_cfg["adminPasswordSalt"] = ""
    return config


def queue_stats_for_source(rows, source_name):
    src = str(source_name or "").strip()
    scoped = [r for r in rows if str(r.get("source", "")).strip() == src]
    total = len(scoped)
    todo = len([r for r in scoped if r.get("status") == "todo"])
    added = len([r for r in scoped if r.get("status") == "added"])
    skipped = len([r for r in scoped if r.get("status") == "skipped"])
    failed = len([r for r in scoped if r.get("status") == "failed"])
    return {"total": total, "todo": todo, "added": added, "skipped": skipped, "failed": failed}


def push_run_history(config, entry):
    app_cfg = config.setdefault("app", {})
    history = app_cfg.setdefault("runHistory", [])
    history.insert(0, entry)
    del history[80:]


def refresh_run_history(config, rows):
    app_cfg = config.setdefault("app", {})
    history = app_cfg.setdefault("runHistory", [])
    changed = False
    for run in history:
        source_name = run.get("listName", "")
        stats = queue_stats_for_source(rows, source_name)
        run["done"] = int(stats["total"] - stats["todo"])
        run["total"] = int(stats["total"])
        run["added"] = int(stats["added"])
        run["skipped"] = int(stats["skipped"])
        run["failed"] = int(stats["failed"])
        if stats["total"] > 0 and stats["todo"] == 0 and run.get("status") != "completed":
            run["status"] = "completed"
            run["completedAt"] = utc_now()
            changed = True
    return changed


def send_webhook_notification(config, event_type, title, detail="", payload_extra=None):
    app_cfg = config.get("app", {})
    if not bool(app_cfg.get("notifyEnabled")):
        return
    webhook = str(app_cfg.get("notifyWebhookUrl", "")).strip()
    if not webhook:
        return
    parsed = urlparse(webhook)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("Webhook URL must be a valid http(s) URL.")
    payload = {
        "event": str(event_type or "info"),
        "title": str(title or ""),
        "detail": str(detail or ""),
        "at": utc_now(),
    }
    if isinstance(payload_extra, dict):
        payload["data"] = payload_extra
    body = json.dumps(payload).encode("utf-8")
    request = Request(webhook, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "Driparr-Notifier/1.0")
    with urlopen(request, timeout=8) as _response:
        return


def save_config(config):
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def read_queue():
    ensure_data()
    with QUEUE_PATH.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_queue(rows):
    DATA.mkdir(exist_ok=True)
    with QUEUE_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def service_request(service, method, path, payload=None):
    base_url = service.get("url", "").rstrip("/")
    if not base_url:
        raise RuntimeError("URL is missing.")

    data = None
    headers = {"X-Api-Key": service.get("apiKey", ""), "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(f"{base_url}/api/v3{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body}") from error
    except URLError as error:
        raise RuntimeError(f"Not reachable: {error.reason}") from error
    except ValueError as error:
        raise RuntimeError(f"Invalid URL or port: {base_url}") from error


def fetch_text(url, timeout=30, retries=2):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    }
    last_error = None
    for _ in range(retries + 1):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError) as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"Failed to fetch list for {url}: {last_error}")


def tmdb_ids_from_text(text):
    ids = []
    for match in re.findall(r"(?:themoviedb\.org/movie/|tmdb[:=\s]+)(\d+)", text, flags=re.I):
        ids.append(match)
    if text.strip().isdigit():
        ids.append(text.strip())
    return list(dict.fromkeys(ids))


def imdb_ids_from_text(text):
    return list(dict.fromkeys(re.findall(r"tt\d{7,9}", text, flags=re.I)))


def imdb_list_id_from_url(url):
    match = re.search(r"/list/(ls\d+)", url)
    return match.group(1) if match else None


def imdb_ids_from_url(url):
    list_id = imdb_list_id_from_url(url)
    candidates = []
    if list_id:
        candidates.append(f"https://www.imdb.com/list/{list_id}/export")
        candidates.append(f"https://www.imdb.com/list/{list_id}/")
    candidates.append(url)

    found = []
    for candidate in candidates:
        try:
            body = fetch_text(candidate, timeout=35, retries=2)
        except RuntimeError:
            continue
        found.extend(imdb_ids_from_text(body))
        for script_blob in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", body, flags=re.I | re.S):
            found.extend(imdb_ids_from_text(script_blob))
        if found:
            break
    return list(dict.fromkeys(found))


def imdb_entries_from_csv_text(csv_text, media_type="movie"):
    entries = []
    try:
        reader = csv.DictReader(csv_text.splitlines())
        for row in reader:
            const_value = (row.get("Const") or row.get("const") or "").strip()
            title = (
                row.get("Title")
                or row.get("title")
                or row.get("Primary Title")
                or row.get("primaryTitle")
                or row.get("Name")
                or row.get("name")
                or ""
            ).strip()
            year = (row.get("Year") or row.get("year") or "").strip()
            if title and year and year.isdigit():
                title = f"{title} ({year})"
            if re.fullmatch(r"tt\d{7,9}", const_value):
                entries.append({"id": const_value, "title": title or f"{media_type.upper()} {const_value}"})
                continue
            url_value = (row.get("URL") or row.get("url") or "").strip()
            for found_id in imdb_ids_from_text(url_value):
                entries.append({"id": found_id, "title": title or f"{media_type.upper()} {found_id}"})
    except Exception:
        for found_id in imdb_ids_from_text(csv_text):
            entries.append({"id": found_id, "title": f"{media_type.upper()} {found_id}"})
    unique = {}
    for entry in entries:
        if entry["id"] not in unique:
            unique[entry["id"]] = entry
    return list(unique.values())


def normalize_title(value):
    text = str(value or "").lower()
    text = re.sub(r"\(\d{4}\)", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_title_year(value):
    text = str(value or "").strip()
    year = None
    match = re.search(r"\((\d{4})\)\s*$", text)
    if match:
        year = int(match.group(1))
        text = re.sub(r"\(\d{4}\)\s*$", "", text).strip()
    return normalize_title(text), year


def build_radarr_movie_index(config):
    movies = service_request(config["radarr"], "GET", "/movie")
    by_tmdb = set()
    by_imdb = set()
    by_title_year = set()
    for movie in movies or []:
        tmdb_id = movie.get("tmdbId")
        imdb_id = str(movie.get("imdbId") or "").strip().lower()
        title_norm = normalize_title(movie.get("title"))
        year = movie.get("year")
        if tmdb_id:
            by_tmdb.add(str(tmdb_id))
        if re.fullmatch(r"tt\d{7,9}", imdb_id):
            by_imdb.add(imdb_id)
        if title_norm and year:
            by_title_year.add((title_norm, int(year)))
    return {"tmdb": by_tmdb, "imdb": by_imdb, "title_year": by_title_year}


def enqueue_ids(config, media_type, id_kind, ids, source_name):
    rows = read_queue()
    existing = {(row["type"], row["externalId"]) for row in rows}
    radarr_cfg = config.get("radarr", {})
    sonarr_cfg = config.get("sonarr", {})
    can_check_radarr = bool(radarr_cfg.get("url") and radarr_cfg.get("apiKey"))
    can_check_sonarr = bool(sonarr_cfg.get("url") and sonarr_cfg.get("apiKey"))

    radarr_index = None
    if media_type == "movie" and can_check_radarr:
        try:
            radarr_index = build_radarr_movie_index(config)
        except RuntimeError:
            radarr_index = None

    added = 0
    for raw in ids:
        if isinstance(raw, dict):
            external_id = str(raw.get("id", "")).strip()
            display_title = str(raw.get("title", "")).strip()
        else:
            external_id = str(raw).strip()
            display_title = ""
        if not external_id:
            continue
        key = (media_type, f"{id_kind}:{external_id}")
        if key in existing:
            continue
        status = "todo"
        reason = ""
        if media_type == "movie" and can_check_radarr:
            try:
                imdb_id_for_check = external_id.lower() if id_kind == "imdb" else ""
                if radarr_index and imdb_id_for_check and imdb_id_for_check in radarr_index["imdb"]:
                    status = "skipped"
                    reason = "Already exists in Radarr by IMDb ID (filtered during import)."
                if status == "todo":
                    tmdb_for_check = external_id if id_kind == "tmdb" else resolve_tmdb_from_imdb(config, external_id)
                    if tmdb_for_check and (
                        (radarr_index and str(tmdb_for_check) in radarr_index["tmdb"])
                        or radarr_has_tmdb(config, int(tmdb_for_check))
                    ):
                        status = "skipped"
                        reason = "Already exists in Radarr (filtered during import)."
                if status == "todo" and display_title and radarr_index:
                    title_norm, year = parse_title_year(display_title)
                    if title_norm and year and (title_norm, year) in radarr_index["title_year"]:
                        status = "skipped"
                        reason = "Already exists in Radarr by title+year (filtered during import)."
            except RuntimeError:
                pass
        if media_type == "series" and can_check_sonarr:
            try:
                series_info = sonarr_series_id_from_external(config, id_kind, external_id)
                if series_info and series_info.get("tvdbId") and sonarr_has_tvdb(config, int(series_info["tvdbId"])):
                    status = "skipped"
                    reason = "Already exists in Sonarr (filtered during import)."
            except RuntimeError:
                pass
        rows.append(
            {
                "type": media_type,
                "externalId": f"{id_kind}:{external_id}",
                "title": display_title or f"{media_type.upper()} {external_id}",
                "status": status,
                "source": source_name,
                "addedAt": "",
                "error": reason,
            }
        )
        existing.add(key)
        added += 1
    write_queue(rows)
    return added


def import_list(source_type, url, media_type, name):
    config = read_config()
    if source_type == "tmdb":
        raise RuntimeError("TMDb import has been removed in this IMDb-only version.")
    if source_type == "imdb" and not config["app"].get("imdbImporterEnabled", True):
        raise RuntimeError("IMDb import is disabled.")

    if source_type == "imdb":
        ids = imdb_ids_from_url(url)
        id_kind = "imdb"
    else:
        raise RuntimeError("Unknown list type.")
    if not ids:
        raise RuntimeError("No items found in this list. Check the URL/type and try again.")

    return enqueue_ids(config, media_type, id_kind, ids, name or url)


def radarr_payload(config, item):
    kind, value = item["externalId"].split(":", 1)
    if kind == "imdb":
        value = resolve_tmdb_from_imdb(config, value)
        if not value:
            raise RuntimeError(f"IMDb ID kon niet worden verwerkt: {item['externalId']}")
    elif kind != "tmdb":
        raise RuntimeError("Onbekend ID type voor Radarr.")
    radarr = config["radarr"]
    return {
        "tmdbId": int(value),
        "qualityProfileId": int(radarr["qualityProfileId"]),
        "rootFolderPath": radarr["rootFolderPath"],
        "monitored": True,
        "minimumAvailability": radarr["minimumAvailability"],
        "addOptions": {"searchForMovie": bool(radarr["searchOnAdd"])},
    }


def sonarr_lookup_external(config, kind, value):
    sonarr = config["sonarr"]
    term = f"{kind}:{value}"
    return service_request(sonarr, "GET", f"/series/lookup?term={term}")


def sonarr_series_id_from_external(config, kind, value):
    results = sonarr_lookup_external(config, kind, value)
    if isinstance(results, list) and results:
        return results[0]
    return None


def sonarr_payload(config, item):
    kind, value = item["externalId"].split(":", 1)
    sonarr = config["sonarr"]
    series_info = sonarr_series_id_from_external(config, kind, value)
    if not series_info:
            raise RuntimeError(f"Series not found in Sonarr lookup: {item['externalId']}")

    payload = {
        "title": series_info.get("title", item["title"]),
        "qualityProfileId": int(sonarr["qualityProfileId"]),
        "languageProfileId": int(sonarr.get("languageProfileId", 1)),
        "seasonFolder": bool(sonarr.get("seasonFolder", True)),
        "rootFolderPath": sonarr["rootFolderPath"],
        "monitored": True,
        "tvdbId": series_info.get("tvdbId"),
        "addOptions": {"searchForMissingEpisodes": bool(sonarr.get("searchOnAdd", True))},
    }
    if series_info.get("year"):
        payload["year"] = series_info.get("year")
    if series_info.get("titleSlug"):
        payload["titleSlug"] = series_info.get("titleSlug")
    return payload


def resolve_tmdb_from_imdb(config, imdb_id):
    result = service_request(config["radarr"], "GET", f"/movie/lookup?term=imdb:{imdb_id}")
    if isinstance(result, list) and result:
        tmdb_id = result[0].get("tmdbId")
        return str(tmdb_id) if tmdb_id else None
    return None


def radarr_has_tmdb(config, tmdb_id):
    movies = service_request(config["radarr"], "GET", "/movie")
    for movie in movies or []:
        if int(movie.get("tmdbId", 0)) == int(tmdb_id):
            return True
    return False


def response_records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    return []


def radarr_queue_records(config):
    try:
        payload = service_request(config["radarr"], "GET", "/queue?pageSize=1000")
    except RuntimeError:
        payload = service_request(config["radarr"], "GET", "/queue")
    return response_records(payload)


def movie_tmdb_id(config, item):
    kind, value = item["externalId"].split(":", 1)
    return value if kind == "tmdb" else resolve_tmdb_from_imdb(config, value)


def find_movie_by_tmdb(movies, tmdb_id):
    if not tmdb_id:
        return None
    for movie in movies or []:
        try:
            if int(movie.get("tmdbId", 0)) == int(tmdb_id):
                return movie
        except (TypeError, ValueError):
            continue
    return None


def queue_entry_for_movie(records, movie):
    movie_id = movie.get("id") if movie else None
    for entry in records or []:
        if not isinstance(entry, dict):
            continue
        if movie_id is not None and entry.get("movieId") == movie_id:
            return entry
        nested = entry.get("movie")
        if isinstance(nested, dict) and movie_id is not None and nested.get("id") == movie_id:
            return entry
    return None


def queue_entry_is_complete(entry):
    if not isinstance(entry, dict):
        return False
    status_values = [
        entry.get("status"),
        entry.get("trackedDownloadState"),
        entry.get("trackedDownloadStatus"),
        entry.get("downloadClientStatus"),
    ]
    complete_states = {"complete", "completed", "downloaded", "finished", "imported", "seeding"}
    for value in status_values:
        normalized = str(value or "").strip().lower()
        if normalized in complete_states:
            return True
    size = entry.get("size")
    left = entry.get("sizeleft")
    if isinstance(size, (int, float)) and size > 0 and isinstance(left, (int, float)) and left <= 0:
        return True
    percent = entry.get("percentComplete") or entry.get("progress")
    if isinstance(percent, (int, float)) and percent >= 100:
        return True
    return False


def sonarr_has_tvdb(config, tvdb_id):
    series = service_request(config["sonarr"], "GET", "/series")
    for item in series or []:
        if int(item.get("tvdbId", 0)) == int(tvdb_id):
            return True
    return False


def movie_is_completed_in_radarr(config, item):
    tmdb_id = movie_tmdb_id(config, item)
    if not tmdb_id:
        return False
    movies = service_request(config["radarr"], "GET", "/movie")
    movie = find_movie_by_tmdb(movies, tmdb_id)
    if not movie:
        return False
    if bool(movie.get("hasFile")) or bool(movie.get("movieFile")):
        return True
    queue_entry = queue_entry_for_movie(radarr_queue_records(config), movie)
    return queue_entry_is_complete(queue_entry)


def series_is_completed_in_sonarr(config, item):
    kind, value = item["externalId"].split(":", 1)
    series_info = sonarr_series_id_from_external(config, kind, value)
    if not series_info or not series_info.get("tvdbId"):
        return False
    all_series = service_request(config["sonarr"], "GET", "/series")
    target_tvdb = int(series_info["tvdbId"])
    for s in all_series or []:
        if int(s.get("tvdbId", 0)) == target_tvdb:
            stats = s.get("statistics", {}) or {}
            return int(stats.get("episodeFileCount", 0)) > 0
    return False


def item_is_completed(config, item):
    if item["type"] == "movie":
        return movie_is_completed_in_radarr(config, item)
    if item["type"] == "series":
        return series_is_completed_in_sonarr(config, item)
    return False


def current_movie_eta(config, item):
    if item.get("type") != "movie":
        return ""
    try:
        movies = service_request(config["radarr"], "GET", "/movie")
        movie = find_movie_by_tmdb(movies, movie_tmdb_id(config, item))
        if not movie:
            return ""
        entry = queue_entry_for_movie(radarr_queue_records(config), movie)
        if entry:
            eta = entry.get("estimatedCompletionTime") or entry.get("timeleft")
            return format_eta_from_iso(eta)
    except Exception:
        return ""
    return ""


def current_movie_progress(config, item):
    if not item or item.get("type") != "movie":
        return {"eta": "", "percent": None, "status": ""}
    try:
        movies = service_request(config["radarr"], "GET", "/movie")
        movie = find_movie_by_tmdb(movies, movie_tmdb_id(config, item))
        if not movie:
            return {"eta": "", "percent": None, "status": ""}
        entry = queue_entry_for_movie(radarr_queue_records(config), movie)
        if entry:
            eta = entry.get("estimatedCompletionTime") or entry.get("timeleft")
            eta_text = format_eta_from_iso(eta)
            size = entry.get("size")
            left = entry.get("sizeleft")
            percent = None
            if isinstance(size, (int, float)) and size > 0 and isinstance(left, (int, float)):
                percent = max(0, min(100, int(round((1 - (left / size)) * 100))))
            percent_value = entry.get("percentComplete") or entry.get("progress")
            if percent is None and isinstance(percent_value, (int, float)):
                percent = max(0, min(100, int(round(percent_value))))
            if queue_entry_is_complete(entry):
                percent = 100
            status = entry.get("status") or entry.get("trackedDownloadState") or "Downloading"
            return {"eta": eta_text, "percent": percent, "status": str(status)}
    except Exception:
        pass
    return {"eta": "", "percent": None, "status": ""}


def discover_radarr_options(service):
    profiles = service_request(service, "GET", "/qualityprofile")
    folders = service_request(service, "GET", "/rootfolder")
    return {
        "profiles": [{"id": p.get("id"), "name": p.get("name")} for p in (profiles or [])],
        "folders": [{"path": f.get("path")} for f in (folders or [])],
    }


def process_once(force=False):
    config = read_config()
    rows = read_queue()
    changed = False
    drip_mode = config["app"].get("dripMode", "sync")

    if drip_mode == "sync" and not force:
        last_added = None
        for row in rows:
            if row.get("status") == "added":
                if not last_added or row.get("addedAt", "") > last_added.get("addedAt", ""):
                    last_added = row
        if last_added:
            try:
                if not item_is_completed(config, last_added):
                    LAST_RUN.update(
                        {
                            "at": utc_now(),
                            "message": f"Sync mode: waiting for completion of {last_added['title']}.",
                        }
                    )
                    return
            except RuntimeError as error:
                LAST_RUN.update({"at": utc_now(), "message": f"Sync check error: {error}"})
                return

    max_items = int(config["app"].get("maxItemsPerRun", 1))
    selected = [row for row in rows if row["status"] == "todo"][:max_items]
    if not selected:
        LAST_RUN.update({"at": utc_now(), "message": "No todo items."})
        return

    for item in selected:
        try:
            if item["type"] == "movie":
                payload = radarr_payload(config, item)
                if radarr_has_tmdb(config, payload["tmdbId"]):
                    item["status"] = "skipped"
                    item["error"] = "Already exists in Radarr (duplicate prevented)."
                    item["addedAt"] = utc_now()
                    changed = True
                    LAST_RUN.update({"at": item["addedAt"], "message": f"Skipped (already present): {item['title']}"})
                    push_event("skipped", item["title"], "Already exists in Radarr")
                    continue
                service_request(config["radarr"], "POST", "/movie", payload)
            elif item["type"] == "series":
                if not config.get("sonarr", {}).get("enabled", False):
                    raise RuntimeError("Sonarr is disabled in settings.")
                payload = sonarr_payload(config, item)
                tvdb_id = payload.get("tvdbId")
                if tvdb_id and sonarr_has_tvdb(config, int(tvdb_id)):
                    item["status"] = "skipped"
                    item["error"] = "Already exists in Sonarr (duplicate prevented)."
                    item["addedAt"] = utc_now()
                    changed = True
                    LAST_RUN.update({"at": item["addedAt"], "message": f"Skipped (already present): {item['title']}"})
                    push_event("skipped", item["title"], "Already exists in Sonarr")
                    continue
                service_request(config["sonarr"], "POST", "/series", payload)
            else:
                raise RuntimeError(f"Unknown media type: {item['type']}")
            item["status"] = "added"
            item["addedAt"] = utc_now()
            item["error"] = ""
            changed = True
            LAST_RUN.update({"at": item["addedAt"], "message": f"Added: {item['title']}"})
            push_event("added", item["title"], "Added to downloader")
        except RuntimeError as error:
            error_text = str(error)
            if "IMDb ID could not be processed" in error_text or "IMDb ID kon niet worden verwerkt" in error_text:
                item["status"] = "skipped"
                item["error"] = f"Unresolvable: {error_text}"
                item["addedAt"] = utc_now()
                changed = True
                LAST_RUN.update({"at": item["addedAt"], "message": f"Skipped (unresolvable): {item['title']}"})
                push_event("skipped", item["title"], "Unresolvable IMDb mapping")
            else:
                item["status"] = "failed"
                item["error"] = error_text
                changed = True
                LAST_RUN.update({"at": utc_now(), "message": f"Error for {item['title']}: {error}"})
                push_event("failed", item["title"], error_text)
                try:
                    send_webhook_notification(config, "run_failed", item["title"], error_text, {"externalId": item.get("externalId", "")})
                except Exception:
                    pass
    write_queue(rows)
    if changed:
        if refresh_run_history(config, rows):
            try:
                send_webhook_notification(config, "list_completed", "List completed", "A list has no open todo items left.")
            except Exception:
                pass
        save_config(config)


def worker_loop():
    ensure_data()
    while True:
        config = read_config()
        if config["app"].get("workerEnabled"):
            with LOCK:
                process_once()
        if config["app"].get("dripMode", "sync") == "sync":
            time.sleep(env_sync_poll_seconds())
        else:
            interval = max(1, int(config["app"].get("intervalMinutes", 60)))
            time.sleep(interval * 60)


def issue_session(username):
    token = secrets.token_urlsafe(24)
    issued = int(time.time())
    payload = f"{username}:{token}:{issued}"
    signature = hmac.new(env_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    session_id = f"{payload}:{signature}"
    SESSIONS[token] = {"username": username, "issued": issued}
    return session_id


def validate_session(session_id):
    try:
        username, token, issued_s, signature = session_id.split(":", 3)
        payload = f"{username}:{token}:{issued_s}"
        expected = hmac.new(env_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        session = SESSIONS.get(token)
        if not session:
            return False
        if int(time.time()) - int(issued_s) > SESSION_TTL_SECONDS:
            SESSIONS.pop(token, None)
            return False
        return True
    except Exception:
        return False


def login_page(error=""):
    err = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    forced_language = env_force_language()
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Driparr Login</title>
<style>
:root {{ --bg:#070214; --panel:#141025; --line:#5941a6; --text:#f3efff; --muted:#a596c9; --accent:#7d4bff; --accent2:#5e2eea; --danger:#ff6b8f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; font-family:Segoe UI,Arial,sans-serif; background:radial-gradient(circle at 30% 20%,#1c1143 0,#0c0622 45%,#050212 100%); color:var(--text); }}
.card {{ width:min(520px,92vw); border:1px solid #3c2d73; border-radius:16px; padding:34px; background:linear-gradient(160deg,rgba(255,255,255,.06),rgba(255,255,255,.02)); box-shadow:0 20px 90px rgba(84,46,205,.35); }}
.logo-wrap {{ width:90px; height:90px; margin:0 auto 16px; border-radius:18px; padding:6px; background:radial-gradient(circle at 30% 30%,rgba(160,106,255,.45),rgba(67,18,185,.25) 70%); border:1px solid #5f42a8; box-shadow:0 10px 30px rgba(120,60,255,.38); }}
.logo {{ width:100%; height:100%; border-radius:12px; object-fit:cover; display:block; }}
h1 {{ text-align:center; margin:0 0 8px; font-size:44px; letter-spacing:.4px; }}
h2 {{ text-align:center; margin:0 0 28px; color:#d3c8f2; font-weight:600; }}
label {{ display:block; margin:14px 0 8px; color:#b9a9e5; font-weight:700; }}
input {{ width:100%; border:1px solid #5e47aa; border-radius:10px; padding:14px 12px; background:#160f2b; color:var(--text); }}
input:focus {{ outline:none; border-color:#8e6bff; box-shadow:0 0 0 3px rgba(125,75,255,.2); }}
button {{ margin-top:20px; width:100%; border:0; border-radius:10px; padding:13px; color:white; background:linear-gradient(140deg,var(--accent),var(--accent2)); font-size:16px; font-weight:800; cursor:pointer; }}
.error {{ margin:10px 0 0; color:var(--danger); font-weight:700; }}
</style></head>
<body><form method=\"POST\" action=\"/login\" class=\"card\"><div class=\"logo-wrap\"><img class=\"logo\" src=\"/assets/rabbit.svg\" alt=\"Driparr logo\"></div><h1>Driparr</h1><h2 id=\"signin\">Sign In</h2><p style=\"text-align:center;color:#b9a9e5;margin:-8px 0 14px;font-size:13px\">Username and password are case-sensitive.</p><label id=\"userLabel\">Username</label><input name=\"username\" autocomplete=\"username\" required><label id=\"passLabel\">Password</label><input name=\"password\" type=\"password\" autocomplete=\"current-password\" required>{err}<button id=\"signinBtn\" type=\"submit\">Sign In</button></form><script>
const forcedLang='{forced_language}';const l=(navigator.language||'en').toLowerCase();const k=forcedLang||(l.startsWith('nl')?'nl':(l.startsWith('de')?'de':'en'));
const t={{en:{{s:'Sign In',u:'Username',p:'Password'}},nl:{{s:'Inloggen',u:'Gebruikersnaam',p:'Wachtwoord'}},de:{{s:'Anmelden',u:'Benutzername',p:'Passwort'}}}}[k];
document.documentElement.lang=k;signin.textContent=t.s;userLabel.textContent=t.u;passLabel.textContent=t.p;signinBtn.textContent=t.s;
</script></body></html>"""


def settings_form(name, values, include_actions=True):
    checked = "checked" if values.get("enabled") else ""
    actions = (
        f"""<div class="actions"><button class="btn secondary" onclick="testService('{name}')">Test</button><button class="btn" onclick="saveService('{name}')">Save</button></div>"""
        if include_actions
        else ""
    )
    quality_field = (
        f"""<div class="service-row"><label>Quality Profile</label><div class="inline-actions"><select id="{name}Quality"><option value="{html.escape(str(values.get('qualityProfileId',1)))}">Current ID {html.escape(str(values.get('qualityProfileId',1)))}</option></select><button class="btn secondary" type="button" onclick="discoverRadarrProfiles()">Refresh profiles</button></div></div>"""
        if name == "radarr"
        else f"""<div class="service-row"><label>Quality Profile ID</label><input id="{name}Quality" type="number" value="{html.escape(str(values.get('qualityProfileId',1)))}"></div>"""
    )
    return f"""<div class="panel service-form"><div class="service-grid">
<div class="service-row"><label>Enabled</label><label class="switch"><input id="{name}Enabled" class="switch-input" type="checkbox" {checked}><span class="switch-slider"></span></label></div>
<div class="service-row"><label>URL *</label><input id="{name}Url" value="{html.escape(values.get('url',''))}" placeholder="http://radarr:7878"><div id="{name}UrlError" class="field-error"></div></div>
<div class="service-row"><label>API key *</label><input id="{name}ApiKey" value="{html.escape(values.get('apiKey',''))}" placeholder="API key"><div id="{name}ApiKeyError" class="field-error"></div><div class="conn-inline"><span id="{name}ConnIndicator" class="conn-indicator"><span class="conn-dot"></span><span id="{name}ConnText">Not tested yet</span></span><button class="btn secondary" type="button" onclick="quickCheckService('{name}')">Check now</button></div></div>
{quality_field}
<div class="service-row"><label>Root Folder</label><input id="{name}Root" value="{html.escape(values.get('rootFolderPath',''))}" placeholder="/movies"></div>
</div>{actions}</div>"""


def page(config, queue):
    def connected(service_cfg):
        if not service_cfg.get("enabled"):
            return False
        if not service_cfg.get("url") or not service_cfg.get("apiKey"):
            return False
        try:
            service_request(service_cfg, "GET", "/system/status")
            return True
        except Exception:
            return False
    def present_queue_reason(row, todo_positions):
        status = str(row.get("status", "")).strip().lower()
        external_id = str(row.get("externalId", "")).strip()
        if status == "todo":
            pos = todo_positions.get(external_id)
            if isinstance(pos, int):
                return f"In queue, position #{pos} for next drip"
            return "In queue for next drip"
        if status == "skipped":
            return "Already in library"
        reason = str(row.get("error", "")).strip()
        if not reason:
            return ""
        lowered = reason.lower()
        legacy_reason_map = {
            "bestaat al in radarr op imdb id (gefilterd bij import).": "Already exists in Radarr by IMDb ID (filtered during import).",
            "bestaat al in radarr (gefilterd bij import).": "Already exists in Radarr (filtered during import).",
            "bestaat al in radarr op titel+jaar (gefilterd bij import).": "Already exists in Radarr by title+year (filtered during import).",
            "bestaat al in sonarr (gefilterd bij import).": "Already exists in Sonarr (filtered during import).",
            "bestaat al in radarr (duplicate voorkomen).": "Already exists in Radarr (duplicate prevented).",
            "bestaat al in sonarr (duplicate voorkomen).": "Already exists in Sonarr (duplicate prevented).",
        }
        if lowered in legacy_reason_map:
            return legacy_reason_map[lowered]
        if lowered.startswith("unresolvable: imdb could not be translated to tmdb:"):
            return ""
        if lowered.startswith("unresolvable: imdb id could not be processed:"):
            return ""
        if lowered.startswith("imdb id could not be processed:"):
            return ""
        if lowered.startswith("niet resolvebaar: imdb kon niet naar tmdb worden vertaald:"):
            return ""
        if lowered.startswith("niet resolvebaar: imdb id kon niet worden verwerkt:"):
            return ""
        if lowered.startswith("imdb id kon niet worden verwerkt:"):
            return ""
        return reason

    radarr_connected = connected(config.get("radarr", {}))
    sonarr_connected = connected(config.get("sonarr", {}))
    worker_enabled = bool(config.get("app", {}).get("workerEnabled", False))
    has_lists = bool(config.get("lists"))
    has_queue_items = bool(queue)
    step1_done = radarr_connected
    step2_done = has_lists or has_queue_items
    step3_done = config.get("app", {}).get("dripMode") in ("timed", "sync")
    step4_done = worker_enabled
    todo = [r for r in queue if r.get("status") == "todo"]
    completed = [r for r in queue if r.get("status") == "added"]
    skipped = [r for r in queue if r.get("status") == "skipped"]
    failed = [r for r in queue if r.get("status") == "failed"]
    todo_positions = {}
    pos_counter = 1
    for row in queue:
        if str(row.get("status", "")).strip().lower() == "todo":
            key = str(row.get("externalId", "")).strip()
            if key:
                todo_positions[key] = pos_counter
                pos_counter += 1
    queue_rows = "\n".join(
        f"<tr><td>{html.escape(row['type'])}</td><td>{html.escape(row['title'])}</td><td>{html.escape(row['externalId'])}</td><td><span class='pill {html.escape(row['status'])}'>{html.escape(row['status'])}</span></td><td>{html.escape(row['source'])}</td><td>{html.escape(present_queue_reason(row, todo_positions))}</td></tr>"
        for row in queue[-140:]
    )
    queued_rows = "\n".join(
        f"<li class='timeline-item timeline-queued'><span class='dot'><img src='/assets/wall-clock.svg' alt='Queued'></span><div><span class='badge b-orange'>Queued</span><span>{html.escape(r['title'])}</span><small>{html.escape(r['externalId'])}</small></div></li>"
        for r in todo[:2]
    ) or "<li class='timeline-item timeline-queued'><span class='dot'><img src='/assets/wall-clock.svg' alt='Queued'></span><div><span class='badge b-orange'>Queued</span><span>No todo items</span><small>Queue is empty</small></div></li>"
    latest_completed = sorted(
        [r for r in completed if r.get("addedAt")],
        key=lambda x: x.get("addedAt", ""),
        reverse=True,
    )
    current_item = latest_completed[0] if latest_completed else None
    progress = current_movie_progress(config, current_item) if current_item else {"eta": "", "percent": None, "status": ""}
    current_eta = progress.get("eta", "")
    progress_percent = progress.get("percent")
    progress_status = progress.get("status", "")
    total_items = len(queue)
    done_items = len(completed)
    skipped_items = len(skipped)
    summary_text = f"{done_items}/{total_items} - skipped {skipped_items}" if total_items else "0/0 - skipped 0"
    event_rows = "\n".join(
        f"<li><span><span class='pill event-pill {html.escape(e.get('level','info'))}'>{html.escape(e.get('level','info'))}</span> {html.escape(e.get('title',''))}</span><small>{html.escape(e.get('detail',''))}</small></li>"
        for e in LAST_EVENTS[:10]
    ) or "<li><span>No events yet</span><small>Driparr actions will appear here.</small></li>"
    current_progress_html = (
        f"<div class='progress-wrap'><div class='progress-label'>{html.escape(progress_status)} {html.escape(current_eta)}</div><div class='progress-track'><div class='progress-fill' style='width:{int(progress_percent)}%'></div></div></div>"
        if current_item and isinstance(progress_percent, int)
        else (f"<small>{html.escape(current_eta)}</small>" if current_item else "<small></small>")
    )
    current_drip_row = (
        f"<li class='timeline-item timeline-current'><span class='dot'><img src='/assets/water-drop.svg' alt='Current'></span><div><span class='badge b-blue'>Current</span><span>{html.escape(current_item['title'])}</span>{current_progress_html}</div></li>"
        if current_item
        else "<li class='timeline-item timeline-current'><span class='dot'><img src='/assets/water-drop.svg' alt='Current'></span><div><span class='badge b-blue'>Current</span><span>No active drip yet</span><small></small></div></li>"
    )
    done_rows = "\n".join(
        f"<li class='timeline-item timeline-completed'><span class='dot'><img src='/assets/wall-clock.svg' alt='Completed'></span><div><span class='badge b-green'>Completed</span><span>{html.escape(r['title'])}</span><small class='completed-time' data-ts='{html.escape(r.get('addedAt',''))}'></small></div></li>"
        for r in latest_completed[1:3]
    ) or "<li class='timeline-item timeline-completed'><span class='dot'><img src='/assets/wall-clock.svg' alt='Completed'></span><div><span class='badge b-green'>Completed</span><span>Nothing added yet</span><small>Run the worker to start</small></div></li>"
    app_cfg = config.get("app", {})
    show_onboarding = (not bool(app_cfg.get("setupComplete"))) or (
        bool(app_cfg.get("setupComplete")) and not bool(app_cfg.get("onboardingDismissed", False))
    )
    skipped_rows = "\n".join(
        f"<li><span>{html.escape(r['title'])}</span><small><img src='/assets/check.svg' alt='In library' style='width:14px;height:14px;display:block;filter:invert(72%) sepia(28%) saturate(889%) hue-rotate(89deg) brightness(95%) contrast(92%);'></small></li>"
        for r in skipped[:12]
    ) or "<li><span>No existing movies detected</span><small><img src='/assets/check.svg' alt='Done' style='width:14px;height:14px;display:block;filter:invert(72%) sepia(28%) saturate(889%) hue-rotate(89deg) brightness(95%) contrast(92%);'></small></li>"
    progress_by_source = {}
    for row in queue:
        source = str(row.get("source", "")).strip()
        if not source:
            continue
        bucket = progress_by_source.setdefault(source, {"total": 0, "done": 0, "todo": 0})
        bucket["total"] += 1
        if row.get("status") == "todo":
            bucket["todo"] += 1
        else:
            bucket["done"] += 1
    list_rows = "\n".join(
        (
            f"<tr><td>{i + 1}</td><td>{html.escape(item.get('name',''))}</td><td>{html.escape(item.get('type',''))}</td><td>{html.escape(item.get('mediaType',''))}</td><td>{html.escape(item.get('url','CSV import'))}</td><td><span class='pill {'added' if ((progress_by_source.get(str(item.get('name','')).strip(),{}).get('todo',0)==0) and (progress_by_source.get(str(item.get('name','')).strip(),{}).get('total',0)>0)) else 'todo'}'>{progress_by_source.get(str(item.get('name','')).strip(),{}).get('done',0)}/{progress_by_source.get(str(item.get('name','')).strip(),{}).get('total',0)}{' done' if ((progress_by_source.get(str(item.get('name','')).strip(),{}).get('todo',0)==0) and (progress_by_source.get(str(item.get('name','')).strip(),{}).get('total',0)>0)) else ''}</span></td><td><button class='btn danger' onclick='deleteList({i})'>Delete</button></td></tr>"
        )
        for i, item in enumerate(config.get("lists", []))
    ) or "<tr><td colspan='7' style='color:#a49ac2'>No saved lists yet</td></tr>"
    run_history_rows = "\n".join(
        f"<tr><td>{html.escape(format_time_only(r.get('at')))}</td><td>{html.escape(r.get('listName',''))}</td><td>{int(r.get('done',0))}/{int(r.get('total',0))}</td><td><span class='pill {'added' if r.get('status') == 'completed' else 'todo'}'>{html.escape(r.get('status','queued'))}</span></td><td>{int(r.get('added',0))}/{int(r.get('skipped',0))}/{int(r.get('failed',0))}</td></tr>"
        for r in config.get("app", {}).get("runHistory", [])[:12]
    ) or "<tr><td colspan='5' style='color:#a49ac2'>No run history yet</td></tr>"
    return f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Driparr</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700;800&display=swap');
:root {{ --bg:#090315; --panel:#141026; --line:#3b2d70; --text:#f1ecff; --muted:#9f92c9; --accent:#8a5fff; --green:#2fdd8f; --yellow:#ffbe3d; --red:#ff6f8f; }}
* {{ box-sizing:border-box; }} body {{ margin:0; font-family:'Space Grotesk',sans-serif; color:var(--text); background:radial-gradient(circle at 35% 0,#1a1041 0,#09031d 50%,#060214 100%); letter-spacing:.1px; }}
.app {{ display:grid; grid-template-columns:260px 1fr; min-height:100vh; }} aside {{ background:linear-gradient(180deg,#2f135f,#1f0e45); border-right:1px solid #50358a; padding:14px 10px; }}
.brand {{ display:flex; align-items:center; gap:10px; padding:8px 10px 16px; font-size:28px; font-weight:800; }} .logo-img {{ width:36px; height:36px; border-radius:9px; object-fit:contain; padding:3px; border:1px solid #6c54b7; background:#200f42; }}
nav button {{ width:100%; border:0; text-align:left; color:#d8cfff; background:transparent; border-radius:10px; padding:12px 12px; font-weight:700; cursor:pointer; margin:4px 0; display:flex; align-items:center; gap:9px; transition:background .22s ease, box-shadow .22s ease, color .22s ease; }}
nav button.active, nav button:hover {{ background:linear-gradient(90deg,#5125a3,#3e1c86); color:white; box-shadow:0 8px 24px rgba(74,36,148,.35); }} .nav-icon {{ width:18px; height:18px; stroke:currentColor; fill:none; stroke-width:1.8; }}
main {{ padding:26px; max-width:1320px; width:100%; margin:0 auto; }} h1 {{ margin:0 0 8px; font-size:38px; color:#f8f5ff; line-height:1.08; }} h1 span {{ color:var(--accent); }} .sub {{ color:var(--muted); margin:0 0 18px; line-height:1.45; }}
.tab {{ display:none; }} .tab.active {{ display:block; }} .panel,.stat {{ background:linear-gradient(160deg,rgba(255,255,255,.07),rgba(255,255,255,.015)); border:1px solid var(--line); border-radius:12px; }}
.panel {{ padding:18px; margin-top:14px; box-shadow:0 10px 36px rgba(0,0,0,.26); transition:box-shadow .24s ease,border-color .24s ease,transform .24s ease; }} .panel:hover {{ box-shadow:0 16px 40px rgba(0,0,0,.36); border-color:#4d3b82; transform:translateY(-1px); }} .statrow {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; margin:14px 0; }}
.stat {{ padding:14px; }} .stat b {{ display:block; margin-top:5px; font-size:24px; }} .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
label {{ display:block; margin:10px 0 6px; font-weight:700; color:#cabdf0; }} input,select {{ width:100%; border:1px solid #4d3a86; border-radius:9px; padding:11px 10px; color:var(--text); background:#120b26; transition:.2s ease; }}
input:focus,select:focus {{ outline:none; border-color:#8661eb; box-shadow:0 0 0 3px rgba(134,97,235,.22); }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }} .btn {{ border:0; border-radius:10px; padding:11px 14px; color:white; background:linear-gradient(130deg,#8f62ff,#6635e8); font-weight:800; cursor:pointer; transition:.2s ease; box-shadow:0 10px 22px rgba(99,53,232,.35); }}
.btn:hover {{ transform:translateY(-1px); box-shadow:0 14px 28px rgba(99,53,232,.42); filter:brightness(1.05); }}
.btn.secondary {{ background:#26163f; border:1px solid #4a3b67; box-shadow:none; }} .btn.secondary:hover {{ box-shadow:0 10px 22px rgba(0,0,0,.3); }} .btn.danger {{ background:#d73b58; }}
.file-input-hidden {{ position:absolute !important; width:1px !important; height:1px !important; opacity:0 !important; pointer-events:none !important; }}
.file-picker {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; width:100%; }}
.file-trigger {{ display:inline-flex; align-items:center; gap:8px; border:1px solid #5d48a0; background:linear-gradient(135deg,#2b1a4b,#24153f); color:#efe8ff; border-radius:10px; padding:10px 14px; font-weight:700; cursor:pointer; transition:.2s ease; }}
.file-trigger:hover {{ border-color:#8a6cff; box-shadow:0 8px 24px rgba(93,72,160,.35); transform:translateY(-1px); }}
.file-chip {{ flex:1; min-width:220px; border:1px dashed #4d3a86; background:#120b26; border-radius:10px; padding:10px 12px; color:#bdb1e3; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.file-chip.has-file {{ border-style:solid; border-color:#6e54c5; color:#e9e1ff; background:#1a1330; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:10px; border-bottom:1px solid #312453; text-align:left; }} th {{ color:#a79ac9; font-size:12px; text-transform:uppercase; letter-spacing:.35px; }}
.pill {{ display:inline-block; border-radius:7px; padding:3px 8px; font-size:12px; font-weight:700; }} .pill.todo {{ background:#392c11; color:var(--yellow); }} .pill.added {{ background:#123425; color:var(--green); }} .pill.failed {{ background:#3a1320; color:var(--red); }} .pill.skipped {{ background:#2d2742; color:#cbb9ff; }}
.feed-list {{ background:#100a20; border:1px solid #2f2450; border-radius:12px; padding:12px; }}
.feed-list h3 {{ margin:2px 0 10px; font-size:16px; }} .feed-list ul {{ list-style:none; margin:0; padding:0; max-height:270px; overflow:auto; }} .feed-list li {{ display:flex; justify-content:space-between; gap:10px; padding:7px 6px; border-bottom:1px solid #2a1f44; }}
.feed-list li span {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }} .feed-list li small {{ color:#a49ac2; font-size:12px; }}
.dashboard-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
.drip-card {{ padding:0; overflow:hidden; }}
.drip-header {{ display:flex; justify-content:space-between; align-items:center; padding:14px 16px; border-bottom:1px solid #2e244a; }}
.drip-stats {{ color:#d2c7f6; font-size:13px; font-weight:700; }}
.view-all {{ color:#9f7cff; font-weight:700; cursor:pointer; text-decoration:none; }}
.timeline {{ padding:10px 14px 16px; }}
.timeline ul {{ list-style:none; margin:0; padding:0; }}
.timeline-item {{ display:grid; grid-template-columns:26px 1fr; gap:10px; align-items:start; padding:11px 6px; border-bottom:1px solid #2a1f44; }}
.timeline-item .dot {{ width:26px; height:26px; border-radius:50%; display:grid; place-items:center; background:#120d1f; border:2px solid #f3b544; box-shadow:0 0 14px rgba(243,181,68,.45); }}
.timeline-item .dot img {{ width:12px; height:12px; object-fit:contain; filter: brightness(0) invert(1); opacity: .98; }}
.timeline-current .dot {{ border-color:#4c85ff; box-shadow:0 0 14px rgba(76,133,255,.5); }}
.timeline-completed .dot {{ border-color:#35d68b; box-shadow:0 0 14px rgba(53,214,139,.45); }}
.timeline-item div span:last-of-type {{ display:block; margin-top:2px; }}
.timeline-item div small {{ display:block; color:#a49ac2; font-size:12px; margin-top:2px; }}
.badge {{ display:inline-flex; align-items:center; gap:6px; padding:2px 8px; border-radius:7px; font-size:12px; font-weight:700; margin-right:6px; }}
.badge img {{ width:12px; height:12px; object-fit:contain; }}
.b-orange {{ background:#3f2d11; color:#ffbe3d; }} .b-blue {{ background:#14254f; color:#8fb2ff; }} .b-green {{ background:#113826; color:#66e7aa; }}
.line-title {{ display:block; }} .line-sub {{ color:#a49ac2; font-size:12px; }}
.progress-wrap {{ margin-top:6px; }}
.progress-label {{ font-size:12px; color:#9cb8ff; margin-bottom:4px; }}
.progress-track {{ height:7px; border-radius:999px; background:#1f2d55; border:1px solid #37508a; overflow:hidden; }}
.progress-fill {{ height:100%; background:linear-gradient(90deg,#56a2ff,#7a66ff); box-shadow:0 0 10px rgba(86,162,255,.45); }}
.event-pill.info {{ background:#1a2d52; color:#7db0ff; }}
.event-pill.added {{ background:#113826; color:#66e7aa; }}
.event-pill.skipped {{ background:#3f2d11; color:#ffbe3d; }}
.event-pill.failed {{ background:#3a1320; color:#ff809c; }}
.instance-card {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }} .instance-title {{ font-size:22px; font-weight:800; }} .enabled-pill {{ background:#123c2a; color:#3fe08f; padding:4px 8px; border-radius:7px; font-size:12px; font-weight:700; }} .disabled-pill {{ background:#3b1622; color:#ff7f96; padding:4px 8px; border-radius:7px; font-size:12px; font-weight:700; border:1px solid #7f3247; }} .instance-actions {{ display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }}
.connected-pill {{ margin-left:auto; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:700; }}
.connected-on {{ background:#123c2a; color:#3fe08f; border:1px solid #1d6546; }}
.connected-off {{ background:#3b1622; color:#ff7f96; border:1px solid #7f3247; }}
.toast {{ position:fixed; right:18px; bottom:18px; background:#22143a; border:1px solid #544078; color:white; border-radius:8px; padding:12px; display:none; }}
.service-status {{ margin-top:10px; padding:10px 12px; border-radius:10px; border:1px solid #3f315f; background:#19122c; color:#d8cfff; font-size:13px; }}
.service-status.busy {{ border-color:#5f4aa0; color:#d8c9ff; }}
.service-status.ok {{ border-color:#2c6b4b; background:#12261d; color:#82eab8; }}
.service-status.error {{ border-color:#8a3149; background:#2a121b; color:#ff9db0; }}
.conn-inline {{ display:flex; align-items:center; gap:10px; margin-top:8px; }}
.conn-indicator {{ display:inline-flex; align-items:center; gap:7px; padding:5px 9px; border:1px solid #4d3a7b; border-radius:999px; background:#18112a; font-size:12px; color:#cfc3ee; }}
.conn-dot {{ width:8px; height:8px; border-radius:999px; background:#8f84b3; box-shadow:0 0 0 3px rgba(143,132,179,.15); }}
.conn-indicator.ok {{ border-color:#2c6b4b; color:#82eab8; }}
.conn-indicator.ok .conn-dot {{ background:#3fe08f; box-shadow:0 0 0 3px rgba(63,224,143,.18); }}
.conn-indicator.busy {{ border-color:#5f4aa0; color:#d8c9ff; }}
.conn-indicator.busy .conn-dot {{ background:#9d8dff; box-shadow:0 0 0 3px rgba(157,141,255,.18); }}
.conn-indicator.error {{ border-color:#8a3149; color:#ff9db0; }}
.conn-indicator.error .conn-dot {{ background:#ff7f96; box-shadow:0 0 0 3px rgba(255,127,150,.16); }}
.modal-backdrop {{ display:none; position:fixed; inset:0; background:rgba(5,3,12,.75); backdrop-filter: blur(3px); z-index:2500; align-items:center; justify-content:center; padding:18px; animation:fadeIn .18s ease; }}
.modal-backdrop.open {{ display:flex; }}
.modal-card {{ width:min(760px,96vw); background:linear-gradient(160deg,#19112d,#120a20); border:1px solid #4b3680; border-radius:14px; box-shadow:0 24px 70px rgba(0,0,0,.52); overflow:hidden; animation:riseIn .2s ease; }}
.modal-head {{ display:flex; justify-content:space-between; align-items:center; padding:14px 16px; border-bottom:1px solid #2f214c; }}
.modal-head b {{ font-size:28px; }}
.modal-close {{ background:transparent; border:0; color:#bca9e8; font-size:24px; cursor:pointer; }}
.modal-body {{ padding:14px 16px 0; max-height:70vh; overflow:auto; }}
.modal-foot {{ position:sticky; bottom:0; display:flex; justify-content:space-between; align-items:center; gap:10px; padding:12px 16px; border-top:1px solid #2f214c; background:linear-gradient(160deg,#171027,#120a20); }}
.field-error {{ color:#ff6f8f; font-size:12px; min-height:16px; margin-top:4px; }}
.is-invalid {{ border-color:#ff6b8f !important; box-shadow:0 0 0 3px rgba(255,107,143,.2) !important; }}
.ob-status {{ margin-top:8px; font-size:12px; color:#cbbdf0; }}
.ob-status.ok {{ color:#82eab8; }}
.ob-status.error {{ color:#ff9db0; }}
.ob-inline {{ display:flex; align-items:end; gap:8px; }}
.service-form {{ padding:16px; border:1px solid #4b3a79; border-radius:12px; background:linear-gradient(145deg,#22173c,#1a1230); }}
.service-grid {{ display:grid; grid-template-columns:1fr; gap:6px; }}
.service-row {{ display:block; }}
.inline-actions {{ display:flex; gap:8px; align-items:center; }}
.inline-actions select {{ flex:1; min-width:200px; }}
.onboarding-list {{ margin:8px 0 0; padding-left:0; color:#d8cfff; line-height:1.6; list-style:none; }}
.onboarding-list li {{ margin:9px 0; display:flex; align-items:center; gap:10px; justify-content:space-between; }}
.onboarding-item-left {{ display:flex; align-items:center; gap:10px; }}
.onb-check {{ width:20px; height:20px; border-radius:999px; border:1px solid #5b4a84; background:#1a1330; display:grid; place-items:center; }}
.onb-check img {{ width:12px; height:12px; opacity:.25; filter:brightness(0) invert(1); }}
.onb-check.done {{ border-color:#2f7d58; background:#103523; box-shadow:0 0 12px rgba(64,220,144,.35); }}
.onb-check.done img {{ opacity:1; }}
.onboarding-setup {{ margin-top:12px; border:1px solid #3d2f63; border-radius:10px; padding:12px; background:#140d25; }}
.onboarding-setup .grid {{ grid-template-columns:1fr 1fr; }}
.onb-icon {{ width:18px; height:18px; object-fit:contain; filter:brightness(0) invert(1); opacity:.96; }}
.onb-link {{ filter:invert(74%) sepia(52%) saturate(680%) hue-rotate(189deg) brightness(95%) contrast(92%); }}
.onb-add {{ filter:invert(78%) sepia(58%) saturate(684%) hue-rotate(76deg) brightness(93%) contrast(91%); }}
.onb-drip {{ filter:invert(69%) sepia(57%) saturate(1662%) hue-rotate(205deg) brightness(97%) contrast(101%); }}
.onb-toggle {{ filter:invert(74%) sepia(49%) saturate(865%) hue-rotate(308deg) brightness(97%) contrast(98%); }}
.slogan {{ margin-top:12px; font-weight:800; color:#d4c8ff; }}
.worker-toggle-wrap {{ display:flex; align-items:center; gap:10px; padding:10px 12px; border:1px solid #3e2f64; border-radius:10px; background:#161028; }}
.worker-toggle-wrap small {{ color:#cfc5ec; font-weight:700; }}
.worker-toggle-wrap .state-pill {{ padding:2px 9px; border-radius:999px; font-size:12px; font-weight:700; border:1px solid #2f7d58; background:#103523; color:#40dc90; }}
.worker-toggle-wrap .state-pill.off {{ border-color:#7f3247; background:#3b1622; color:#ff7f96; }}
.worker-actions {{ display:flex; align-items:center; gap:10px; }}
.queue-meta {{ margin-left:auto; margin-right:8px; font-size:12px; color:#bfb2e5; }}
.btn:disabled {{ opacity:.45; cursor:not-allowed; box-shadow:none; }}
.switch {{ position:relative; display:inline-block; width:44px; height:24px; }}
.switch-input {{ opacity:0; width:0; height:0; }}
.switch-slider {{ position:absolute; inset:0; border-radius:999px; background:#372956; border:1px solid #5c4a7a; transition:.2s; }}
.switch-slider:before {{ content:""; position:absolute; width:18px; height:18px; left:2px; top:2px; background:white; border-radius:50%; transition:.2s; }}
.switch-input:checked + .switch-slider {{ background:#5f3cc6; border-color:#7d5de0; }}
.switch-input:checked + .switch-slider:before {{ transform:translateX(20px); }}
.tab.active {{ animation:fadeSlide .16s ease; }}
.modal-close:hover {{ color:#ffffff; }}
@keyframes fadeIn {{ from {{ opacity:0; }} to {{ opacity:1; }} }}
@keyframes riseIn {{ from {{ opacity:0; transform:translateY(6px) scale(.99); }} to {{ opacity:1; transform:translateY(0) scale(1); }} }}
@keyframes fadeSlide {{ from {{ opacity:0; transform:translateY(4px); }} to {{ opacity:1; transform:translateY(0); }} }}
.queue-wrap {{ overflow:auto; border-radius:10px; }}
.guided-focus {{ border-color:#7c63d6 !important; box-shadow:0 0 0 2px rgba(124,99,214,.28), 0 18px 44px rgba(19,10,42,.45) !important; animation:guidedPulse 1.2s ease 2; }}
@keyframes guidedPulse {{ 0% {{ transform:translateY(0); }} 50% {{ transform:translateY(-2px); }} 100% {{ transform:translateY(0); }} }}
@media (max-width:1200px) {{
  h1 {{ font-size:34px; }}
  main {{ padding:20px; }}
}}
@media (max-width:980px) {{
  .app {{ grid-template-columns:1fr; }}
  aside {{ position:sticky; top:0; z-index:20; padding:10px; backdrop-filter: blur(8px); }}
  .brand {{ font-size:24px; padding:4px 8px 10px; }}
  nav {{ display:flex; gap:8px; overflow:auto; padding-bottom:4px; }}
  nav button {{ min-width:max-content; padding:10px 12px; margin:0; }}
  nav button .connected-pill {{ display:none; }}
  main {{ padding:14px; }}
  .grid,.statrow {{ grid-template-columns:1fr; }}
  .dashboard-grid {{ grid-template-columns:1fr; }}
  .panel {{ padding:14px; margin-top:10px; border-radius:11px; }}
  h1 {{ font-size:28px; }}
  .sub {{ margin-bottom:12px; font-size:14px; }}
  .modal-card {{ width:min(720px,98vw); }}
  .modal-head b {{ font-size:24px; }}
  .timeline {{ padding:8px 10px 12px; }}
  .timeline-item {{ gap:8px; padding:10px 4px; }}
  .drip-header {{ padding:12px; }}
  .instance-card {{ flex-direction:column; align-items:flex-start; }}
  .instance-actions {{ width:100%; justify-content:flex-start; }}
  .actions .btn {{ width:100%; }}
  .worker-toggle-wrap {{ width:100%; justify-content:space-between; }}
}}
</style></head>
<body><div class=\"app\"><aside><div class=\"brand\"><img class=\"logo-img\" src=\"/assets/rabbit.svg\" alt=\"Driparr\">Driparr</div><nav>
<button class=\"active\" data-tab=\"dashboard\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M3 13h8V3H3v10zm10 8h8V3h-8v18zM3 21h8v-6H3v6z\"/></svg>Dashboard</button>
<button data-tab=\"lists\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01\"/></svg>Lists</button>
<button data-tab=\"queue\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M4 7h16M4 12h16M4 17h10\"/></svg>Queue</button>
<button data-tab=\"radarr\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"m8 5 11 7-11 7V5z\"/></svg>Radarr<span class=\"connected-pill {'connected-on' if radarr_connected else 'connected-off'}\">{'Connected' if radarr_connected else 'Offline'}</span></button>
<button data-tab=\"sonarr\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M12 3l4.5 2.6v5.2L12 13.4 7.5 10.8V5.6L12 3zM7.5 13.2 12 15.8l4.5-2.6v5.2L12 21l-4.5-2.6v-5.2z\"/></svg>Sonarr<span class=\"connected-pill {'connected-on' if sonarr_connected else 'connected-off'}\">{'Connected' if sonarr_connected else 'Offline'}</span></button>
<button data-tab=\"general\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M12 15.5A3.5 3.5 0 1 0 12 8.5a3.5 3.5 0 0 0 0 7z\"/><path d=\"M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 0 1-4 0v-.08a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 0 1 0-4h.08a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01a1.7 1.7 0 0 0 1-1.55V3a2 2 0 0 1 4 0v.08a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01a1.7 1.7 0 0 0 1.55 1H21a2 2 0 0 1 0 4h-.08a1.7 1.7 0 0 0-1.55 1z\"/></svg>General</button>
<button onclick=\"logout()\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4\"/><path d=\"m16 17 5-5-5-5\"/><path d=\"M21 12H9\"/></svg>Logout</button>
</nav></aside><main>
<section id=\"dashboard\" class=\"tab active\"><h1 data-i18n=\"dashboard\">Dashboard</h1><p class=\"sub\" data-i18n=\"dashboard_sub\">Live overzicht van queue, huidige drip en afgeronde items.</p>
<div class=\"panel drip-card\">
  <div class=\"drip-header\"><b data-i18n=\"drip_timeline\">Drip Timeline</b><span class=\"drip-stats\">{summary_text}</span><a class=\"view-all\" href=\"#\" onclick=\"openTab('queue');return false;\" data-i18n=\"view_all_queue\">View all queue</a></div>
  <div class=\"timeline\">
    <div class=\"line-sub\" style=\"margin:4px 0 8px\" data-i18n=\"next_queued\">Next 2 queued</div>
    <ul>
      {queued_rows}
    </ul>
    <div class=\"line-sub\" style=\"margin:10px 0 8px\" data-i18n=\"current_drip\">Current drip</div>
    <ul>
      {current_drip_row}
    </ul>
    <div class=\"line-sub\" style=\"margin:10px 0 8px\" data-i18n=\"last_completed\">Last 2 completed</div>
    <ul>
      {done_rows}
    </ul>
  </div>
  <div class=\"actions\" style=\"padding:0 14px 14px\">
    <div class=\"worker-toggle-wrap\">
      <div class=\"worker-actions\">
        <span data-i18n=\"worker_state_label\">Worker</span>
        <label class=\"switch\">
          <input id=\"workerEnabled\" class=\"switch-input\" type=\"checkbox\" {'checked' if worker_enabled else ''} onchange=\"saveGeneral(true)\">
          <span class=\"switch-slider\"></span>
        </label>
        <small id=\"workerStateText\" class=\"state-pill\" data-i18n=\"worker_state_on\">Start</small>
      </div>
      <span class=\"queue-meta\">Queue: {len(queue)} items</span>
      <button class=\"btn secondary\" onclick=\"forceNext()\" {'disabled' if len(queue) == 0 else ''}>Force Next</button>
      <button class=\"btn secondary\" onclick=\"clearQueue()\" {'disabled' if len(queue) == 0 else ''}>Clear all</button>
    </div>
  </div>
</div>
<div class=\"dashboard-grid\">
<div class=\"panel\"><h3 style=\"margin-top:0\">Recent Events</h3><p class=\"sub\">What Driparr has done recently.</p><div class=\"feed-list\"><ul>{event_rows}</ul></div></div>
<div class=\"panel\"><h3 style=\"margin-top:0\" data-i18n=\"already_library\">Already in Library</h3><p class=\"sub\" data-i18n=\"already_library_sub\">Automatically skipped to prevent duplicates.</p><div class=\"feed-list\"><ul>{skipped_rows}</ul></div></div>
</div>
</section>
<section id=\"lists\" class=\"tab\"><h1>Lists</h1><p class=\"sub\">Import an IMDb CSV file.</p><div class=\"panel\"><div class=\"grid\"><div><label>Name *</label><input id=\"listName\" placeholder=\"My list\" required><div id=\"listNameError\" class=\"field-error\"></div></div><div><label>Media</label><select id=\"listMedia\"><option value=\"movie\">Movies</option><option value=\"series\">Series</option></select></div></div><h3 style=\"margin-top:12px\">IMDb CSV Import</h3><p class=\"sub\">Choose your IMDb export CSV and upload it directly.</p><div class=\"actions\" style=\"align-items:center;gap:10px\"><input id=\"imdbCsvFile\" class=\"file-input-hidden\" type=\"file\" accept=\".csv,text/csv\" onchange=\"onImdbCsvSelected(this)\"><div class=\"file-picker\"><label for=\"imdbCsvFile\" class=\"file-trigger\">Choose file</label><div id=\"imdbCsvFileMeta\" class=\"file-chip\">No file selected</div></div><button id=\"imdbCsvUploadBtn\" class=\"btn secondary\" onclick=\"importImdbCsvFile()\">Upload CSV</button></div><div id=\"imdbCsvProgressWrap\" style=\"display:none;margin-top:10px\"><div style=\"height:10px;background:#2c2449;border:1px solid #4d3a86;border-radius:999px;overflow:hidden\"><div id=\"imdbCsvProgressBar\" style=\"height:100%;width:0%;background:linear-gradient(90deg,#8f62ff,#6635e8)\"></div></div><div id=\"imdbCsvProgressText\" class=\"sub\" style=\"margin-top:6px\">0%</div></div></div><div class=\"panel\"><h3 style=\"margin-top:0\">Saved lists</h3><div class=\"queue-wrap\"><table><thead><tr><th>#</th><th>Name</th><th>Type</th><th>Media</th><th>Source</th><th>Status</th><th>Action</th></tr></thead><tbody>{list_rows}</tbody></table></div></div><div class=\"panel\"><h3 style=\"margin-top:0\">Run history</h3><div class=\"queue-wrap\"><table><thead><tr><th>Time</th><th>List</th><th>Progress</th><th>Status</th><th>A/S/F</th></tr></thead><tbody>{run_history_rows}</tbody></table></div></div></section>
<section id=\"queue\" class=\"tab\"><h1>Queue</h1><p class=\"sub\">All items and statuses.</p><div class=\"panel\"><div class=\"queue-wrap\"><table><thead><tr><th>Type</th><th>Title</th><th>ID</th><th>Status</th><th>Source</th><th>Reason</th></tr></thead><tbody>{queue_rows}</tbody></table></div></div></section>
<section id=\"radarr\" class=\"tab\"><h1>Radarr <span>Settings</span></h1><p class=\"sub\">Manage your Radarr instances</p><div class=\"panel\"><div class=\"instance-title\" style=\"margin-bottom:12px\">Instances</div><div class=\"instance-card\"><div><b>Radarr</b> <span class=\"{'enabled-pill' if config['radarr'].get('enabled') else 'disabled-pill'}\">{'Enabled' if config['radarr'].get('enabled') else 'Disabled'}</span> <span style=\"color:#9f92c9;margin-left:10px\">{html.escape(config['radarr'].get('url',''))}</span></div><div class=\"instance-actions\"><button class=\"btn\" onclick=\"openModal('radarrModal')\">Add / Edit Instance</button><button class=\"btn secondary\" onclick=\"testService('radarr')\">Test</button></div></div></div></section>
<section id=\"sonarr\" class=\"tab\"><h1>Sonarr <span>Settings</span></h1><p class=\"sub\">Manage your Sonarr instances</p><div class=\"panel\"><div class=\"instance-title\" style=\"margin-bottom:12px\">Instances</div><div class=\"instance-card\"><div><b>Sonarr</b> <span class=\"{'enabled-pill' if config['sonarr'].get('enabled') else 'disabled-pill'}\">{'Enabled' if config['sonarr'].get('enabled') else 'Disabled'}</span> <span style=\"color:#9f92c9;margin-left:10px\">{html.escape(config['sonarr'].get('url',''))}</span></div><div class=\"instance-actions\"><button class=\"btn\" onclick=\"openModal('sonarrModal')\">Add / Edit Instance</button><button class=\"btn secondary\" onclick=\"testService('sonarr')\">Test</button></div></div></div></section>
<section id=\"general\" class=\"tab\"><h1 data-i18n=\"general\">General</h1><div class=\"panel\"><div class=\"grid\"><div><label data-i18n=\"drip_mode\">Drip mode</label><select id=\"dripMode\"><option value=\"timed\" data-i18n=\"drip_mode_timed\">Timed (interval based)</option><option value=\"sync\" data-i18n=\"drip_mode_sync\">Sync (wait for completion)</option></select></div><div><label id=\"intervalPresetLabel\" data-i18n=\"drip_interval\">Drip interval</label><select id=\"intervalPreset\" onchange=\"setIntervalFromPreset()\"><option value=\"15\" data-i18n=\"every_15\">Every 15 minutes</option><option value=\"30\" data-i18n=\"every_30\">Every 30 minutes</option><option value=\"60\" data-i18n=\"every_60\">Every hour</option><option value=\"90\" data-i18n=\"every_90\">Every 1.5 hours</option><option value=\"custom\" data-i18n=\"custom\">Custom</option></select></div><div><label id=\"intervalMinutesLabel\" data-i18n=\"interval_custom\">Interval minutes (custom)</label><input id=\"intervalMinutes\" type=\"number\" value=\"{config['app'].get('intervalMinutes')}\"></div><div><label data-i18n=\"max_items\">Max items per run</label><input id=\"maxItemsPerRun\" type=\"number\" value=\"{config['app'].get('maxItemsPerRun')}\"></div><div><label>Webhook notifications</label><label class=\"switch\"><input id=\"notifyEnabled\" class=\"switch-input\" type=\"checkbox\" {'checked' if config['app'].get('notifyEnabled', False) else ''}><span class=\"switch-slider\"></span></label></div><div><label>Webhook URL</label><input id=\"notifyWebhookUrl\" value=\"{html.escape(config['app'].get('notifyWebhookUrl',''))}\" placeholder=\"https://example.com/webhook\"></div><div><label data-i18n=\"language\">Language</label><select id=\"languagePref\"><option value=\"auto\" data-i18n=\"language_auto\">Auto (system)</option><option value=\"en\">English</option><option value=\"nl\">Nederlands</option><option value=\"de\">Deutsch</option></select></div></div><div class=\"actions\"><button class=\"btn\" onclick=\"saveGeneral()\" data-i18n=\"save\">Save</button><button class=\"btn secondary\" onclick=\"testNotification()\">Test notification</button><button class=\"btn secondary\" onclick=\"showOnboardingAgain()\" data-i18n=\"show_checklist\">Show checklist</button></div></div></section>
</main></div>
<div id=\"radarrModal\" class=\"modal-backdrop\"><div class=\"modal-card\"><div class=\"modal-head\"><b>Add Instance</b><button class=\"modal-close\" onclick=\"closeModal('radarrModal')\">×</button></div><div class=\"modal-body\">{settings_form('radarr', config['radarr'], include_actions=False)}</div><div class=\"modal-foot\"><button class=\"btn secondary\" onclick=\"testService('radarr')\">Test</button><button class=\"btn\" onclick=\"saveService('radarr')\">Save</button></div></div></div>
<div id=\"sonarrModal\" class=\"modal-backdrop\"><div class=\"modal-card\"><div class=\"modal-head\"><b>Add Instance</b><button class=\"modal-close\" onclick=\"closeModal('sonarrModal')\">×</button></div><div class=\"modal-body\">{settings_form('sonarr', config['sonarr'], include_actions=False)}</div><div class=\"modal-foot\"><button class=\"btn secondary\" onclick=\"testService('sonarr')\">Test</button><button class=\"btn\" onclick=\"saveService('sonarr')\">Save</button></div></div></div>
<div id=\"onboardingModal\" class=\"modal-backdrop{' open' if show_onboarding else ''}\"><div class=\"modal-card\"><div class=\"modal-head\"><b data-i18n=\"onboarding_title\">Quick Start Checklist</b><button class=\"modal-close\" onclick=\"dismissOnboarding()\">×</button></div><div class=\"modal-body\"><p class=\"sub\" data-i18n=\"onboarding_sub\">Follow these steps once and you're ready.</p><ol class=\"onboarding-list\"><li><div class=\"onboarding-item-left\"><img class=\"onb-icon onb-link\" src=\"/assets/link.svg\" alt=\"link\"><span data-i18n=\"onboarding_1\">Connect Radarr.</span></div><span class=\"onb-check {'done' if step1_done else ''}\"><img src=\"/assets/check.svg\" alt=\"done\"></span></li><li><div class=\"onboarding-item-left\"><img class=\"onb-icon onb-add\" src=\"/assets/add.svg\" alt=\"add\"><span data-i18n=\"onboarding_2\">Import an IMDb CSV list.</span></div><span class=\"onb-check {'done' if step2_done else ''}\"><img src=\"/assets/check.svg\" alt=\"done\"></span></li><li><div class=\"onboarding-item-left\"><img class=\"onb-icon onb-drip\" src=\"/assets/water-drop.svg\" alt=\"drip\"><span data-i18n=\"onboarding_3\">Choose drip mode (timed or sync) and interval.</span></div><span class=\"onb-check {'done' if step3_done else ''}\"><img src=\"/assets/check.svg\" alt=\"done\"></span></li><li><div class=\"onboarding-item-left\"><img class=\"onb-icon onb-toggle\" src=\"/assets/toggle.svg\" alt=\"toggle\"><span data-i18n=\"onboarding_4\">Enable worker and monitor the timeline.</span></div><span class=\"onb-check {'done' if step4_done else ''}\"><img src=\"/assets/check.svg\" alt=\"done\"></span></li></ol><div class=\"onboarding-setup\"><div class=\"grid\"><div><label>Radarr URL *</label><input id=\"obRadarrUrl\" value=\"{html.escape(config.get('radarr', {}).get('url',''))}\" placeholder=\"http://radarr:7878\"><div id=\"obRadarrUrlError\" class=\"field-error\"></div></div><div><label>Radarr API key *</label><input id=\"obRadarrApi\" value=\"{html.escape(config.get('radarr', {}).get('apiKey',''))}\" placeholder=\"API key\"><div id=\"obRadarrApiError\" class=\"field-error\"></div></div></div><div class=\"grid\"><div><label>Quality Profile</label><select id=\"obRadarrQuality\" disabled><option value=\"\">Test first...</option></select><div id=\"obQualityStatus\" class=\"ob-status\">Test the connection first to fetch profiles.</div></div><div class=\"ob-inline\"><button class=\"btn secondary\" type=\"button\" onclick=\"testOnboardingRadarr()\">Test</button></div></div></div><p class=\"slogan\" data-i18n=\"set_and_forget\">Set and forget.</p></div><div class=\"modal-foot\"><span></span><button class=\"btn onboarding-save\" onclick=\"saveOnboardingSetup()\">Save</button></div></div></div>
<div id=\"toast\" class=\"toast\"></div>
<script>
function openTab(tabId) {{ document.querySelectorAll('nav button').forEach(b => b.classList.remove('active')); document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); const btn = document.querySelector(`nav button[data-tab="${{tabId}}"]`); if (btn) btn.classList.add('active'); const tab = document.getElementById(tabId); if (tab) tab.classList.add('active'); }}
function openModal(id) {{
  const m=document.getElementById(id);
  if (m) m.classList.add('open');
  if (id === 'radarrModal') discoverRadarrProfiles();
  if (id === 'radarrModal') wireServiceHealth('radarr');
  if (id === 'sonarrModal') wireServiceHealth('sonarr');
}}
function closeModal(id) {{ const m=document.getElementById(id); if (m) m.classList.remove('open'); }}
function detectLanguage() {{
  const forcedLang = '{env_force_language()}';
  if (forcedLang) return forcedLang;
  const pref = localStorage.getItem('driparr_lang_pref') || 'auto';
  if (pref !== 'auto') return pref;
  const raw = (navigator.language || navigator.userLanguage || 'en').toLowerCase();
  if (raw.startsWith('nl')) return 'nl';
  if (raw.startsWith('de')) return 'de';
  return 'en';
}}
function applyI18n() {{
  const lang = detectLanguage();
  const dict = {{
    en: {{
      dashboard:'Dashboard', dashboard_sub:'Live overview of queue, current drip and completed items.',
      drip_timeline:'Visual Drip Timeline', view_all_queue:'View all queue', next_queued:'Next 2 queued', current_drip:'Current drip to Radarr/Sonarr',
      last_completed:'Last 2 completed drips', worker_state_label:'Start/Pause', worker_state_on:'Start', worker_state_off:'Pause', already_library:'Already in Library',
      already_library_sub:'Automatically skipped to prevent duplicates.', onboarding_title:'Quick Start Checklist',
      onboarding_sub:'Follow these steps once and you are ready.', onboarding_1:'Connect Radarr.',
      onboarding_2:'Import an IMDb CSV list.', onboarding_3:'Choose drip mode (timed or sync) and interval.',
      onboarding_4:'Enable worker and monitor the timeline.', set_and_forget:'Set and forget.', got_it:'Got it',
      general:'General', drip_mode:'Drip mode', drip_mode_timed:'Timed (interval based)', drip_mode_sync:'Sync (wait for completion)',
      drip_interval:'Drip interval', every_15:'Every 15 minutes', every_30:'Every 30 minutes', every_60:'Every hour', every_90:'Every 90 minutes',
      custom:'Custom', interval_custom:'Interval minutes (custom)', max_items:'Max items per run',
      imdb_import:'IMDb import enabled', language:'Language', language_auto:'Auto (system)', save:'Save', show_checklist:'Show checklist'
    }},
    nl: {{
      dashboard:'Dashboard', dashboard_sub:'Live overzicht van queue, huidige drip en afgeronde items.',
      drip_timeline:'Visuele Drip Timeline', view_all_queue:'Bekijk volledige queue', next_queued:'Volgende 2 queued',
      current_drip:'Huidige drip naar Radarr/Sonarr', last_completed:'Laatste 2 afgeronde drips',
      worker_state_label:'Start/Pauze', worker_state_on:'Start', worker_state_off:'Pauze', already_library:'Al in Bibliotheek', already_library_sub:'Automatisch overgeslagen om duplicaten te voorkomen.',
      onboarding_title:'Snelle Start Checklist', onboarding_sub:'Doorloop dit eenmalig en je bent klaar.',
      onboarding_1:'Koppel Radarr.', onboarding_2:'Importeer een IMDb CSV-lijst.',
      onboarding_3:'Kies dripmodus (timed of sync) en interval.', onboarding_4:'Zet de worker aan en volg de timeline.',
      set_and_forget:'Set and forget.', got_it:'Begrepen', general:'Algemeen', drip_mode:'Drip modus',
      drip_mode_timed:'Timed (op interval)', drip_mode_sync:'Sync (wacht op afronding)', drip_interval:'Drip interval',
      every_15:'Elke 15 minuten', every_30:'Elke 30 minuten', every_60:'Elk uur', every_90:'Elke 1,5 uur', custom:'Aangepast',
      interval_custom:'Interval minuten (aangepast)', max_items:'Max items per run',
      imdb_import:'IMDb import actief', language:'Taal', language_auto:'Auto (systeem)', save:'Opslaan', show_checklist:'Toon checklist'
    }},
    de: {{
      dashboard:'Dashboard', dashboard_sub:'Live-Übersicht von Queue, aktuellem Drip und abgeschlossenen Einträgen.',
      drip_timeline:'Visuelle Drip-Zeitleiste', view_all_queue:'Gesamte Queue anzeigen', next_queued:'Nächste 2 in Warteschlange',
      current_drip:'Aktueller Drip zu Radarr/Sonarr', last_completed:'Letzte 2 abgeschlossene Drips',
      worker_state_label:'Start/Pause', worker_state_on:'Start', worker_state_off:'Pause', already_library:'Bereits in Bibliothek', already_library_sub:'Automatisch übersprungen, um Duplikate zu vermeiden.',
      onboarding_title:'Quick-Start Checkliste', onboarding_sub:'Einmal durchgehen, dann bist du bereit.',
      onboarding_1:'Radarr verbinden.', onboarding_2:'Eine IMDb-CSV-Liste importieren.',
      onboarding_3:'Drip-Modus (zeitgesteuert oder sync) und Intervall wählen.', onboarding_4:'Worker aktivieren und Timeline beobachten.',
      set_and_forget:'Set and forget.', got_it:'Verstanden', general:'Allgemein', drip_mode:'Drip-Modus',
      drip_mode_timed:'Zeitgesteuert (Intervall)', drip_mode_sync:'Sync (auf Abschluss warten)', drip_interval:'Drip-Intervall',
      every_15:'Alle 15 Minuten', every_30:'Alle 30 Minuten', every_60:'Jede Stunde', every_90:'Alle 90 Minuten', custom:'Benutzerdefiniert',
      interval_custom:'Intervall Minuten (benutzerdefiniert)', max_items:'Max. Elemente pro Lauf',
      imdb_import:'IMDb-Import aktiv', language:'Sprache', language_auto:'Auto (System)', save:'Speichern', show_checklist:'Checkliste anzeigen'
    }}
  }};
  const t = dict[lang] || dict.en;
  document.querySelectorAll('[data-i18n]').forEach(el => {{
    const key = el.getAttribute('data-i18n');
    if (t[key]) el.textContent = t[key];
  }});
  const navMap = {{
    en: ['Dashboard','Lists','Queue','Radarr','Sonarr','General','Logout'],
    nl: ['Dashboard','Lijsten','Queue','Radarr','Sonarr','Algemeen','Uitloggen'],
    de: ['Dashboard','Listen','Queue','Radarr','Sonarr','Allgemein','Abmelden']
  }};
  document.querySelectorAll('nav button[data-tab], nav button[onclick*="logout"]').forEach((btn, i) => {{
    const pill = btn.querySelector('.connected-pill');
    const label = (navMap[lang] || navMap.en)[i] || btn.textContent;
    btn.childNodes.forEach((n) => {{ if (n.nodeType === 3) n.nodeValue = ''; }});
    const icon = btn.querySelector('svg');
    if (icon) icon.insertAdjacentText('afterend', label);
    if (pill) btn.appendChild(pill);
  }});
  const tabNames = {{
    en: {{lists:'Lists', queue:'Queue', radarr:'Radarr Settings', sonarr:'Sonarr Settings'}},
    nl: {{lists:'Lijsten', queue:'Queue', radarr:'Radarr Instellingen', sonarr:'Sonarr Instellingen'}},
    de: {{lists:'Listen', queue:'Queue', radarr:'Radarr Einstellungen', sonarr:'Sonarr Einstellungen'}}
  }};
  const tn = tabNames[lang] || tabNames.en;
  const h = (id, txt) => {{ const el=document.querySelector(`#${{id}} h1`); if (el) el.textContent = txt; }};
  h('lists', tn.lists); h('queue', tn.queue); h('radarr', tn.radarr); h('sonarr', tn.sonarr);
  const subt = {{
    en: {{lists:'Import an IMDb CSV.', queue:'All items and status.', radarr:'Manage your Radarr instances', sonarr:'Manage your Sonarr instances'}},
    nl: {{lists:'Importeer een IMDb CSV.', queue:'Alle items en status.', radarr:'Beheer je Radarr-instanties', sonarr:'Beheer je Sonarr-instanties'}},
    de: {{lists:'Importiere eine IMDb-CSV.', queue:'Alle Elemente und Status.', radarr:'Verwalte deine Radarr-Instanzen', sonarr:'Verwalte deine Sonarr-Instanzen'}}
  }};
  const st = subt[lang] || subt.en;
  const setSub = (id, text) => {{ const el=document.querySelector(`#${{id}} .sub`); if (el) el.textContent = text; }};
  setSub('lists', st.lists); setSub('queue', st.queue); setSub('radarr', st.radarr); setSub('sonarr', st.sonarr);
  const labels = {{
    en: {{
      name:'Name', type:'Type', media:'Media', imdb_csv:'IMDb CSV Import',
      imdb_csv_sub:'Choose your IMDb export CSV and upload directly.', import_pasted:'Upload CSV',
      queue_type:'Type', queue_title:'Title', queue_id:'ID', queue_status:'Status', queue_source:'Source', queue_reason:'Reason',
      add_edit_instance:'Add / Edit Instance', test:'Test', save:'Save', instances:'Instances', enabled:'Enabled', disabled:'Disabled',
      current:'Current', queued:'Queued', completed:'Completed'
    }},
    nl: {{
      name:'Naam', type:'Type', media:'Media', imdb_csv:'IMDb CSV Import',
      imdb_csv_sub:'Kies je IMDb export CSV en upload direct.', import_pasted:'Upload CSV',
      queue_type:'Type', queue_title:'Titel', queue_id:'ID', queue_status:'Status', queue_source:'Bron', queue_reason:'Reden',
      add_edit_instance:'Toevoegen / Wijzigen', test:'Test', save:'Opslaan', instances:'Instanties', enabled:'Ingeschakeld', disabled:'Uitgeschakeld',
      current:'Huidig', queued:'Queued', completed:'Voltooid'
    }},
    de: {{
      name:'Name', type:'Typ', media:'Medien', imdb_csv:'IMDb CSV Import',
      imdb_csv_sub:'Wahle deine IMDb-Export-CSV und lade sie direkt hoch.', import_pasted:'CSV hochladen',
      queue_type:'Typ', queue_title:'Titel', queue_id:'ID', queue_status:'Status', queue_source:'Quelle', queue_reason:'Grund',
      add_edit_instance:'Hinzufügen / Bearbeiten', test:'Test', save:'Speichern', instances:'Instanzen', enabled:'Aktiviert', disabled:'Deaktiviert',
      current:'Aktuell', queued:'Wartend', completed:'Abgeschlossen'
    }}
  }};
  const l = labels[lang] || labels.en;
  const listLabels = document.querySelectorAll('#lists .panel .grid label');
  if (listLabels[0]) listLabels[0].textContent = l.name;
  if (listLabels[1]) listLabels[1].textContent = l.media;
  const csvHead = document.querySelector('#lists .panel h3');
  if (csvHead) csvHead.textContent = l.imdb_csv;
  const csvSub = document.querySelector('#lists .panel p.sub');
  if (csvSub) csvSub.textContent = l.imdb_csv_sub;
  const csvBtn = document.querySelector('#imdbCsvUploadBtn');
  if (csvBtn) csvBtn.textContent = l.import_pasted;
  const th = document.querySelectorAll('#queue th');
  if (th[0]) th[0].textContent = l.queue_type;
  if (th[1]) th[1].textContent = l.queue_title;
  if (th[2]) th[2].textContent = l.queue_id;
  if (th[3]) th[3].textContent = l.queue_status;
  if (th[4]) th[4].textContent = l.queue_source;
  if (th[5]) th[5].textContent = l.queue_reason;
  document.querySelectorAll('.instance-title').forEach((e) => e.textContent = l.instances);
  document.querySelectorAll('.instance-actions .btn:not(.secondary)').forEach((e) => e.textContent = l.add_edit_instance);
  document.querySelectorAll('.instance-actions .btn.secondary').forEach((e) => e.textContent = l.test);
  document.querySelectorAll('.modal-foot .btn.secondary').forEach((e) => e.textContent = l.test);
  document.querySelectorAll('.modal-foot .btn:not(.secondary):not(.onboarding-save)').forEach((e) => e.textContent = l.save);
  document.querySelectorAll('.enabled-pill').forEach((e) => e.textContent = l.enabled);
  document.querySelectorAll('.disabled-pill').forEach((e) => e.textContent = l.disabled);
  document.querySelectorAll('.b-blue').forEach((e) => e.textContent = l.current);
  document.querySelectorAll('.b-orange').forEach((e) => e.textContent = l.queued);
  document.querySelectorAll('.b-green').forEach((e) => e.textContent = l.completed);
  const conn = {{
    en: {{on:'Connected', off:'Offline'}},
    nl: {{on:'Verbonden', off:'Offline'}},
    de: {{on:'Verbunden', off:'Offline'}}
  }}[lang] || {{on:'Connected', off:'Offline'}};
  document.querySelectorAll('.connected-pill').forEach((e) => {{
    e.textContent = e.classList.contains('connected-on') ? conn.on : conn.off;
  }});
  document.querySelectorAll('.completed-time').forEach((el) => {{
    const ts = el.getAttribute('data-ts');
    if (!ts) return;
    const d = new Date(ts);
    if (!isNaN(d)) el.textContent = new Intl.DateTimeFormat(lang, {{hour:'2-digit', minute:'2-digit'}}).format(d);
  }});
  const pref = document.getElementById('languagePref');
  if (pref) pref.value = localStorage.getItem('driparr_lang_pref') || 'auto';
  const ws = document.getElementById('workerStateText');
  const we = document.getElementById('workerEnabled');
  if (ws && we) {{
    ws.textContent = we.checked ? (t.worker_state_on || 'Start') : (t.worker_state_off || 'Pause');
    ws.classList.toggle('off', !we.checked);
  }}
  document.documentElement.lang = lang;
}}
document.querySelectorAll('nav button[data-tab]').forEach(btn => btn.onclick = () => openTab(btn.dataset.tab));
async function post(url, data) {{
  const r = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data)}});
  if (r.status===401) {{ location.href='/login'; return; }}
  let j = null;
  try {{
    j = await r.json();
  }} catch (_e) {{
    j = {{ok:false, message:`Server antwoordde met HTTP ${{r.status}}.`}};
  }}
  toast(j.message || (j.ok ? 'Saved' : 'Error'));
  if (j.reload) setTimeout(()=>location.reload(), 650);
  return j;
}}
function toast(msg) {{ const t=document.getElementById('toast'); t.textContent=msg; t.style.display='block'; setTimeout(()=>t.style.display='none',3000); }}
function ensureServiceStatusEl(name) {{
  let el = document.getElementById(name + 'ServiceStatus');
  if (el) return el;
  const modal = document.getElementById(name + 'Modal');
  const foot = modal ? modal.querySelector('.modal-foot') : null;
  if (!foot) return null;
  el = document.createElement('div');
  el.id = name + 'ServiceStatus';
  el.className = 'service-status';
  el.style.display = 'none';
  foot.insertBefore(el, foot.firstChild);
  return el;
}}
function setServiceStatus(name, kind, message) {{
  const el = ensureServiceStatusEl(name);
  if (!el) return;
  el.classList.remove('busy','ok','error');
  el.classList.add(kind || 'busy');
  el.textContent = message || '';
  el.style.display = 'block';
}}
function setConnIndicator(name, kind, text) {{
  const wrap = document.getElementById(name + 'ConnIndicator');
  const label = document.getElementById(name + 'ConnText');
  if (!wrap || !label) return;
  wrap.classList.remove('busy','ok','error');
  if (kind) wrap.classList.add(kind);
  label.textContent = text || 'Not tested yet';
}}
function setServiceButtonsBusy(name, busy) {{
  const selectors = [
    `button[onclick="testService('${{name}}')"]`,
    `button[onclick="saveService('${{name}}')"]`
  ];
  selectors.forEach((sel) => {{
    document.querySelectorAll(sel).forEach((btn) => {{
      btn.disabled = !!busy;
    }});
  }});
}}
const serviceHealthTimers = {{}};
const serviceHealthWired = {{}};
function scheduleServiceHealthCheck(name, delayMs) {{
  if (serviceHealthTimers[name]) clearTimeout(serviceHealthTimers[name]);
  serviceHealthTimers[name] = setTimeout(() => {{
    quickCheckService(name, true);
  }}, delayMs || 750);
}}
function wireServiceHealth(name) {{
  if (serviceHealthWired[name]) return;
  const urlEl = document.getElementById(name + 'Url');
  const apiEl = document.getElementById(name + 'ApiKey');
  if (!urlEl || !apiEl) return;
  const onInput = () => {{
    const hasValues = (urlEl.value || '').trim() && (apiEl.value || '').trim();
    if (!hasValues) {{
      setConnIndicator(name, 'error', 'URL/API key missing');
      return;
    }}
    setConnIndicator(name, 'busy', 'Change detected, checking...');
    scheduleServiceHealthCheck(name, 900);
  }};
  urlEl.addEventListener('input', onInput);
  apiEl.addEventListener('input', onInput);
  serviceHealthWired[name] = true;
}}
async function quickCheckService(name, silent) {{
  if (!validateService(name)) return;
  if (!silent) setServiceStatus(name, 'busy', 'Testing connection...');
  setConnIndicator(name, 'busy', 'Testing connection...');
  try {{
    const j = await post('/api/service/'+name+'/test', serviceData(name));
    if (j && j.ok) {{
      setConnIndicator(name, 'ok', 'Connected');
      if (!silent) setServiceStatus(name, 'ok', j.message || 'Connection successful.');
    }} else {{
      setConnIndicator(name, 'error', 'Not connected');
      if (!silent) setServiceStatus(name, 'error', (j && j.message) ? j.message : 'Connection failed.');
    }}
  }} catch (e) {{
    setConnIndicator(name, 'error', 'Not connected');
    if (!silent) setServiceStatus(name, 'error', `Connection failed: ${{e?.message || 'unknown error'}}`);
  }}
}}
function serviceData(name) {{
  const qualityEl = document.getElementById(name+'Quality');
  const qualityProfileId = Number((qualityEl && qualityEl.value) ? qualityEl.value : 1) || 1;
  return {{url:document.getElementById(name+'Url').value, apiKey:document.getElementById(name+'ApiKey').value, qualityProfileId:qualityProfileId, rootFolderPath:document.getElementById(name+'Root').value, enabled:document.getElementById(name+'Enabled').checked}};
}}
async function discoverRadarrProfiles() {{
  const urlEl = document.getElementById('radarrUrl');
  const apiEl = document.getElementById('radarrApiKey');
  if (!urlEl || !apiEl) return;
  const url = (urlEl.value || '').trim();
  const apiKey = (apiEl.value || '').trim();
  if (!url || !apiKey) return;
  const res = await post('/api/radarr/discover', {{url, apiKey}});
  if (!res || !res.ok) return;
  const q = document.getElementById('radarrQuality');
  if (q && Array.isArray(res.profiles) && res.profiles.length) {{
    const current = String(q.value || '');
    q.innerHTML = '';
    res.profiles.forEach((p) => {{
      const opt = document.createElement('option');
      opt.value = String(p.id);
      opt.textContent = `${{p.name}} (ID ${{p.id}})`;
      if (String(p.id) === current) opt.selected = true;
      q.appendChild(opt);
    }});
    if (!q.value) q.value = String(res.profiles[0].id);
  }}
}}
function validateService(name) {{
  let ok = true;
  const url = (document.getElementById(name+'Url').value || '').trim();
  const api = (document.getElementById(name+'ApiKey').value || '').trim();
  const urlErr = document.getElementById(name+'UrlError');
  const apiErr = document.getElementById(name+'ApiKeyError');
  if (urlErr) urlErr.textContent = '';
  if (apiErr) apiErr.textContent = '';
  if (!url) {{ if (urlErr) urlErr.textContent = 'URL is required'; ok = false; }}
  if (!api) {{ if (apiErr) apiErr.textContent = 'API key is required'; ok = false; }}
  return ok;
}}
async function saveService(name) {{
  if (!validateService(name)) return;
  setServiceStatus(name, 'busy', 'Saving and validating...');
  setConnIndicator(name, 'busy', 'Validating...');
  setServiceButtonsBusy(name, true);
  try {{
    const j = await post('/api/service/'+name, serviceData(name));
    if (j && j.ok) {{
      setServiceStatus(name, 'ok', j.message || 'Settings saved.');
      setConnIndicator(name, 'ok', 'Connected');
    }} else {{
      setServiceStatus(name, 'error', (j && j.message) ? j.message : 'Save failed.');
      setConnIndicator(name, 'error', 'Not connected');
    }}
  }} catch (e) {{
    setServiceStatus(name, 'error', `Save failed: ${{e?.message || 'unknown error'}}`);
    setConnIndicator(name, 'error', 'Not connected');
  }} finally {{
    setServiceButtonsBusy(name, false);
  }}
}}
async function testService(name) {{
  if (!validateService(name)) return;
  setServiceStatus(name, 'busy', 'Testing connection...');
  setConnIndicator(name, 'busy', 'Testing connection...');
  setServiceButtonsBusy(name, true);
  try {{
    const j = await post('/api/service/'+name+'/test', serviceData(name));
    if (j && j.ok) {{
      setServiceStatus(name, 'ok', j.message || 'Connection successful.');
      setConnIndicator(name, 'ok', 'Connected');
    }} else {{
      setServiceStatus(name, 'error', (j && j.message) ? j.message : 'Connection failed.');
      setConnIndicator(name, 'error', 'Not connected');
    }}
  }} catch (e) {{
    setServiceStatus(name, 'error', `Connection failed: ${{e?.message || 'unknown error'}}`);
    setConnIndicator(name, 'error', 'Not connected');
  }} finally {{
    setServiceButtonsBusy(name, false);
  }}
}}
function saveGeneral(toggle) {{ post('/api/general', {{dripMode:dripMode.value, intervalMinutes:Number(intervalMinutes.value), maxItemsPerRun:Number(maxItemsPerRun.value), notifyEnabled:(document.getElementById('notifyEnabled') ? document.getElementById('notifyEnabled').checked : false), notifyWebhookUrl:(document.getElementById('notifyWebhookUrl') ? document.getElementById('notifyWebhookUrl').value : ''), toggleWorker:!!toggle, workerEnabled:(document.getElementById('workerEnabled') ? document.getElementById('workerEnabled').checked : undefined)}}); }}
function testNotification() {{ post('/api/notify-test', {{}}); }}
function setIntervalFromPreset() {{ if (intervalPreset.value !== 'custom') intervalMinutes.value = Number(intervalPreset.value); }}
function updateDripModeUI() {{
  const isSync = dripMode.value === 'sync';
  intervalPreset.disabled = isSync;
  intervalMinutes.disabled = isSync;
  const presetLabel = document.getElementById('intervalPresetLabel');
  const minsLabel = document.getElementById('intervalMinutesLabel');
  if (presetLabel) presetLabel.style.opacity = isSync ? '0.55' : '1';
  if (minsLabel) minsLabel.style.opacity = isSync ? '0.55' : '1';
}}
(() => {{ const v = Number(intervalMinutes.value); if ([15,30,60,90].includes(v)) intervalPreset.value = String(v); else intervalPreset.value = 'custom'; }})();
(() => {{ dripMode.value = "{config['app'].get('dripMode','sync')}"; updateDripModeUI(); }})();
dripMode.addEventListener('change', updateDripModeUI);
function addList() {{ post('/api/lists', {{name:listName.value, type:listType.value, mediaType:listMedia.value, url:listUrl.value}}); }}
function validateListName() {{
  const name = (listName.value || '').trim();
  const err = document.getElementById('listNameError');
  if (err) err.textContent = '';
  if (!name) {{
    if (err) err.textContent = 'Name is required';
    return false;
  }}
  return true;
}}
function setCsvProgress(percent, text) {{
  const wrap = document.getElementById('imdbCsvProgressWrap');
  const bar = document.getElementById('imdbCsvProgressBar');
  const label = document.getElementById('imdbCsvProgressText');
  if (!wrap || !bar || !label) return;
  const safe = Math.max(0, Math.min(100, Math.round(percent)));
  wrap.style.display = 'block';
  bar.style.width = `${{safe}}%`;
  label.textContent = text || `${{safe}}%`;
}}
function onImdbCsvSelected(input) {{
  const file = input?.files && input.files[0];
  const meta = document.getElementById('imdbCsvFileMeta');
  if (!file) {{
    if (meta) {{
      meta.textContent = 'No file selected';
      meta.classList.remove('has-file');
    }}
    return;
  }}
  if (!listName.value) listName.value = (file.name || 'IMDb CSV').replace(/\\.csv$/i, '');
  if (meta) {{
    meta.textContent = `${{file.name}} (${{(file.size / 1024).toFixed(1)}} KB)`;
    meta.classList.add('has-file');
  }}
  setCsvProgress(0, 'Ready to upload');
}}
function importImdbCsvFile() {{
  if (!validateListName()) return;
  const fileInput = document.getElementById('imdbCsvFile');
  const file = fileInput?.files && fileInput.files[0];
  if (!file) {{ toast('Choose a CSV file first.'); return; }}
  if (file.size > 2 * 1024 * 1024) {{ toast('CSV file is too large (max 2 MB).'); return; }}
  const uploadBtn = document.getElementById('imdbCsvUploadBtn');
  if (uploadBtn) uploadBtn.disabled = true;
  const reader = new FileReader();
  reader.onprogress = (ev) => {{
    if (!ev.lengthComputable) return;
    setCsvProgress((ev.loaded / ev.total) * 45, 'Reading file...');
  }};
  reader.onerror = () => {{
    if (uploadBtn) uploadBtn.disabled = false;
    toast('CSV could not be read');
  }};
  reader.onload = () => {{
    const payload = JSON.stringify({{
      name: listName.value.trim(),
      mediaType: listMedia.value || 'movie',
      csvText: String(reader.result || '')
    }});
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/import-csv');
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.upload.onprogress = (ev) => {{
      if (!ev.lengthComputable) return;
      const p = 45 + (ev.loaded / ev.total) * 50;
      setCsvProgress(p, 'Uploading...');
    }};
    xhr.onload = () => {{
      if (uploadBtn) uploadBtn.disabled = false;
      if (xhr.status === 401) {{ location.href = '/login'; return; }}
      try {{
        const j = JSON.parse(xhr.responseText || '{{}}');
        setCsvProgress(100, 'Done');
        toast(j.message || (j.ok ? 'Import completed' : 'Import failed'));
        if (j.reload) setTimeout(() => location.reload(), 650);
      }} catch (_err) {{
        toast('Unexpected server response');
      }}
    }};
    xhr.onerror = () => {{
      if (uploadBtn) uploadBtn.disabled = false;
      toast('Upload failed');
    }};
    setCsvProgress(50, 'Uploading...');
    xhr.send(payload);
  }};
  reader.readAsText(file);
}}
function deleteList(index) {{
  if (!confirm('Are you sure you want to delete this list?')) return;
  post('/api/lists/delete', {{index:index}});
}}
function clearQueue() {{
  if (!confirm('Are you sure you want to clear the full queue?')) return;
  post('/api/queue/clear', {{}});
}}
function runNow() {{ post('/api/run-now', {{}}); }}
function forceNext() {{ post('/api/force-next', {{}}); }}
function dismissOnboarding() {{
  const modal = document.getElementById('onboardingModal');
  if (modal) modal.classList.remove('open');
  post('/api/onboarding-dismiss', {{}});
}}
function validateOnboardingRadarrFields() {{
  const urlEl = document.getElementById('obRadarrUrl');
  const apiEl = document.getElementById('obRadarrApi');
  const urlErr = document.getElementById('obRadarrUrlError');
  const apiErr = document.getElementById('obRadarrApiError');
  const url = (urlEl?.value || '').trim();
  const api = (apiEl?.value || '').trim();
  if (urlErr) urlErr.textContent = '';
  if (apiErr) apiErr.textContent = '';
  if (urlEl) urlEl.classList.remove('is-invalid');
  if (apiEl) apiEl.classList.remove('is-invalid');
  let ok = true;
  if (!url) {{
    ok = false;
    if (urlEl) urlEl.classList.add('is-invalid');
    if (urlErr) urlErr.textContent = 'Radarr URL is required.';
  }} else if (!/^https?:\\/\\//i.test(url)) {{
    ok = false;
    if (urlEl) urlEl.classList.add('is-invalid');
    if (urlErr) urlErr.textContent = 'Use http:// or https://';
  }}
  if (!api) {{
    ok = false;
    if (apiEl) apiEl.classList.add('is-invalid');
    if (apiErr) apiErr.textContent = 'Radarr API key is required.';
  }}
  return ok;
}}
async function testOnboardingRadarr() {{
  if (!validateOnboardingRadarrFields()) return;
  const url = (document.getElementById('obRadarrUrl')?.value || '').trim();
  const apiKey = (document.getElementById('obRadarrApi')?.value || '').trim();
  const qualityEl = document.getElementById('obRadarrQuality');
  const statusEl = document.getElementById('obQualityStatus');
  if (qualityEl) {{
    qualityEl.disabled = true;
    qualityEl.innerHTML = '<option value=\"\">Testen...</option>';
  }}
  if (statusEl) {{
    statusEl.classList.remove('ok', 'error');
    statusEl.textContent = 'Testing connection...';
  }}
  const j = await post('/api/radarr/discover', {{url:url, apiKey:apiKey}});
  if (!j || !j.ok) {{
    if (statusEl) {{
      statusEl.classList.remove('ok');
      statusEl.classList.add('error');
      statusEl.textContent = (j && j.message) ? j.message : 'Connection failed.';
    }}
    return;
  }}
  const profiles = Array.isArray(j.profiles) ? j.profiles : [];
  if (!profiles.length) {{
    if (statusEl) {{
      statusEl.classList.remove('ok');
      statusEl.classList.add('error');
      statusEl.textContent = 'No quality profiles found in Radarr.';
    }}
    return;
  }}
  if (qualityEl) {{
    qualityEl.innerHTML = '';
    profiles.forEach((p) => {{
      const opt = document.createElement('option');
      opt.value = String(p.id);
      opt.textContent = `${{p.name}} (ID ${{p.id}})`;
      qualityEl.appendChild(opt);
    }});
    qualityEl.disabled = false;
  }}
  if (statusEl) {{
    statusEl.classList.remove('error');
    statusEl.classList.add('ok');
    statusEl.textContent = 'Connected. Choose your quality profile.';
  }}
}}
function saveOnboardingSetup() {{
  const url = (document.getElementById('obRadarrUrl')?.value || '').trim();
  const apiKey = (document.getElementById('obRadarrApi')?.value || '').trim();
  const qualitySelect = document.getElementById('obRadarrQuality');
  const qualityValue = qualitySelect && !qualitySelect.disabled ? Number(qualitySelect.value || 0) : 0;
  if (!validateOnboardingRadarrFields()) return;
  if (!qualityValue) {{ toast('Test Radarr first and choose a quality profile.'); return; }}
  post('/api/quick-setup', {{radarrUrl:url, radarrApiKey:apiKey, qualityProfileId:qualityValue}}).then((j) => {{
    if (j && j.ok) {{
      dismissOnboarding();
      localStorage.setItem('driparr_next_step', 'lists');
      setTimeout(() => location.reload(), 500);
    }}
  }});
}}
function showOnboardingAgain() {{
  post('/api/onboarding-reset', {{}}).then(() => location.reload());
}}
function resetOnboardingQualityState() {{
  const qualityEl = document.getElementById('obRadarrQuality');
  const statusEl = document.getElementById('obQualityStatus');
  if (qualityEl) {{
    qualityEl.disabled = true;
    qualityEl.innerHTML = '<option value=\"\">Test first...</option>';
  }}
  if (statusEl) {{
    statusEl.classList.remove('ok', 'error');
    statusEl.textContent = 'Test the connection first to fetch profiles.';
  }}
}}
document.getElementById('obRadarrUrl')?.addEventListener('input', resetOnboardingQualityState);
document.getElementById('obRadarrApi')?.addEventListener('input', resetOnboardingQualityState);
const languagePref = document.getElementById('languagePref');
if (languagePref) {{
  languagePref.addEventListener('change', () => {{
    localStorage.setItem('driparr_lang_pref', languagePref.value || 'auto');
    applyI18n();
  }});
}}
async function logout() {{ await post('/api/logout', {{}}); location.href='/login'; }}
applyI18n();
(() => {{
  const next = localStorage.getItem('driparr_next_step');
  if (next === 'lists') {{
    openTab('lists');
    const listsFirstPanel = document.querySelector('#lists .panel');
    if (listsFirstPanel) {{
      listsFirstPanel.classList.add('guided-focus');
      setTimeout(() => listsFirstPanel.classList.remove('guided-focus'), 3200);
    }}
    localStorage.removeItem('driparr_next_step');
  }}
}})();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type="text/html", set_cookie=None, location=None):
        payload = body.encode("utf-8")
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise RuntimeError("Request is too large.")
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            raise RuntimeError("Invalid JSON payload.")

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise RuntimeError("Request is too large.")
        raw = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] if values else "" for key, values in parsed.items()}

    def session_cookie(self):
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return ""
        jar = cookies.SimpleCookie()
        jar.load(cookie_header)
        if "driparr_session" not in jar:
            return ""
        return jar["driparr_session"].value

    def is_authenticated(self):
        session_id = self.session_cookie()
        return validate_session(session_id)

    def auth_required(self):
        if self.is_authenticated():
            return False
        if self.path.startswith("/api/"):
            self.respond(401, json.dumps({"ok": False, "message": "Not authenticated"}), "application/json")
        else:
            self.respond(302, "", location="/login")
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/assets/"):
            asset_name = Path(path.replace("/assets/", "", 1)).name
            asset_path = ASSETS / asset_name
            if asset_path.exists() and asset_path.is_file():
                payload = asset_path.read_bytes()
                content_type = "application/octet-stream"
                if asset_path.suffix.lower() == ".png":
                    content_type = "image/png"
                elif asset_path.suffix.lower() == ".svg":
                    content_type = "image/svg+xml"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.respond(404, "Not found", "text/plain")
            return
        config = read_config()
        if path == "/login":
            self.respond(200, login_page())
            return
        if self.auth_required():
            return
        if path == "/setup":
            self.respond(302, "", location="/")
            return
        queue = read_queue()
        self.respond(200, page(config, queue))

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path.startswith("/assets/"):
            asset_name = Path(path.replace("/assets/", "", 1)).name
            asset_path = ASSETS / asset_name
            if asset_path.exists() and asset_path.is_file():
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
            return
        config = read_config()
        if path == "/login":
            self.send_response(200)
            self.end_headers()
            return
        if not self.is_authenticated():
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if path == "/setup":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            form = self.read_form()
            username = form.get("username", "")
            password = form.get("password", "")
            config = read_config()
            if verify_login(config, username, password):
                session_id = issue_session(username)
                cookie = f"driparr_session={session_id}; HttpOnly; Path=/; SameSite=Lax"
                self.respond(302, "", set_cookie=cookie, location="/")
            else:
                self.respond(200, login_page("Invalid username or password."))
            return
        if path != "/login" and self.auth_required():
            return

        try:
            data = self.read_json()
            config = read_config()

            if path.startswith("/api/service/") and path.endswith("/test"):
                name = path.split("/")[3]
                status = service_request(data, "GET", "/system/status")
                app_name = str((status or {}).get("appName", name.title())).strip() or name.title()
                version = str((status or {}).get("version", "")).strip()
                version_suffix = f" (v{version})" if version else ""
                self.respond(200, json.dumps({"ok": True, "message": f"Connected to {app_name}{version_suffix}."}), "application/json")
                return

            if path == "/api/radarr/discover":
                service = {"url": data.get("url", ""), "apiKey": data.get("apiKey", "")}
                found = discover_radarr_options(service)
                self.respond(200, json.dumps({"ok": True, **found}), "application/json")
                return

            if path.startswith("/api/service/"):
                name = path.split("/")[3]
                config[name].update(data)
                config[name]["enabled"] = bool(config[name].get("enabled"))
                if "qualityProfileId" in config[name]:
                    config[name]["qualityProfileId"] = safe_int(config[name].get("qualityProfileId"), 1, minimum=1)
                if name == "radarr" and not str(config["radarr"].get("rootFolderPath", "")).strip():
                    discovered = discover_radarr_options(config["radarr"])
                    if discovered["folders"]:
                        config["radarr"]["rootFolderPath"] = discovered["folders"][0]["path"]
                save_config(config)
                self.respond(200, json.dumps({"ok": True, "message": f"{name.title()} saved.", "reload": True}), "application/json")
                return

            if path == "/api/general":
                config["app"]["dripMode"] = data.get("dripMode", "sync")
                if config["app"]["dripMode"] not in ("timed", "sync"):
                    config["app"]["dripMode"] = "sync"
                config["app"]["intervalMinutes"] = safe_int(data.get("intervalMinutes"), config["app"].get("intervalMinutes", 60), minimum=1, maximum=24 * 60)
                config["app"]["maxItemsPerRun"] = safe_int(data.get("maxItemsPerRun"), config["app"].get("maxItemsPerRun", 1), minimum=1, maximum=50)
                config["app"]["notifyEnabled"] = bool(data.get("notifyEnabled", False))
                webhook_url = str(data.get("notifyWebhookUrl", "")).strip()
                if webhook_url:
                    parsed = urlparse(webhook_url)
                    if parsed.scheme not in ("http", "https") or not parsed.netloc:
                        raise RuntimeError("Webhook URL must be a valid http(s) URL.")
                config["app"]["notifyWebhookUrl"] = webhook_url
                config["app"]["tmdbImporterEnabled"] = False
                config["app"]["imdbImporterEnabled"] = True
                if "workerEnabled" in data and data.get("workerEnabled") is not None:
                    config["app"]["workerEnabled"] = bool(data.get("workerEnabled"))
                elif data.get("toggleWorker"):
                    config["app"]["workerEnabled"] = not config["app"].get("workerEnabled")
                save_config(config)
                self.respond(200, json.dumps({"ok": True, "message": "Settings saved.", "reload": True}), "application/json")
                return

            if path == "/api/notify-test":
                try:
                    send_webhook_notification(config, "test", "Driparr test", "Webhook notifications are working.")
                    self.respond(200, json.dumps({"ok": True, "message": "Test notification sent."}), "application/json")
                except Exception as error:
                    self.respond(200, json.dumps({"ok": False, "message": f"Test notification failed: {error}"}), "application/json")
                return

            if path == "/api/lists":
                config["lists"].append(data)
                save_config(config)
                added = import_list(data["type"], data["url"], data["mediaType"], data.get("name", ""))
                self.respond(200, json.dumps({"ok": True, "message": f"{added} items imported.", "reload": True}), "application/json")
                return

            if path == "/api/lists/delete":
                index = int(data.get("index", -1))
                lists = config.get("lists", [])
                if index < 0 or index >= len(lists):
                    raise RuntimeError("List not found.")
                removed = lists.pop(index)
                config["lists"] = lists
                save_config(config)
                self.respond(200, json.dumps({"ok": True, "message": f"List deleted: {removed.get('name','(unnamed)')}", "reload": True}), "application/json")
                return

            if path == "/api/import-csv":
                csv_text = data.get("csvText", "")
                media_type = data.get("mediaType", "movie")
                if media_type not in ("movie", "series"):
                    media_type = "movie"
                list_name = str(data.get("name", "")).strip()
                if not list_name:
                    raise RuntimeError("List name is required.")
                if not csv_text.strip():
                    raise RuntimeError("CSV text is empty.")
                if len(csv_text) > MAX_CSV_CHARS:
                    raise RuntimeError("CSV file is too large (max 2 MB).")
                entries = imdb_entries_from_csv_text(csv_text, media_type=media_type)
                if not entries:
                    raise RuntimeError("No IMDb IDs found in CSV.")
                config.setdefault("lists", []).append(
                    {
                        "name": list_name,
                        "type": "imdb_csv",
                        "mediaType": media_type,
                        "url": "CSV import",
                    }
                )
                save_config(config)
                added = enqueue_ids(config, media_type, "imdb", entries, list_name)
                push_run_history(
                    config,
                    {
                        "at": utc_now(),
                        "listName": list_name,
                        "status": "queued",
                        "done": 0,
                        "total": int(added),
                        "added": 0,
                        "skipped": 0,
                        "failed": 0,
                    },
                )
                refresh_run_history(config, read_queue())
                save_config(config)
                worker_enabled = bool(config.get("app", {}).get("workerEnabled"))
                if worker_enabled:
                    with LOCK:
                        process_once(force=True)
                    message = f"{added} items imported from CSV. First drip has started."
                else:
                    message = f"{added} items imported from CSV. Worker is paused, nothing has been dripped yet."
                push_event("info", f"CSV imported: {list_name}", f"{added} items queued")
                try:
                    send_webhook_notification(config, "list_imported", f"List imported: {list_name}", f"{added} items queued", {"listName": list_name, "queued": added})
                except Exception:
                    pass
                self.respond(200, json.dumps({"ok": True, "message": message, "reload": True}), "application/json")
                return

            if path == "/api/run-now":
                if not bool(config.get("app", {}).get("workerEnabled")):
                    self.respond(200, json.dumps({"ok": True, "message": "Worker is paused. Turn Start on first.", "reload": False}), "application/json")
                    return
                with LOCK:
                    process_once()
                push_event("info", "Manual run", LAST_RUN["message"])
                self.respond(200, json.dumps({"ok": True, "message": LAST_RUN["message"], "reload": True}), "application/json")
                return

            if path == "/api/force-next":
                with LOCK:
                    process_once(force=True)
                push_event("info", "Force next", LAST_RUN["message"])
                self.respond(200, json.dumps({"ok": True, "message": LAST_RUN["message"], "reload": True}), "application/json")
                return

            if path == "/api/queue/clear":
                write_queue([])
                refresh_run_history(config, [])
                save_config(config)
                LAST_RUN.update({"at": utc_now(), "message": "Queue manually cleared."})
                push_event("info", "Queue cleared", "All queued items removed by user")
                self.respond(200, json.dumps({"ok": True, "message": "Queue has been cleared.", "reload": True}), "application/json")
                return

            if path == "/api/onboarding-dismiss":
                config["app"]["onboardingDismissed"] = True
                save_config(config)
                self.respond(200, json.dumps({"ok": True, "message": "Checklist hidden."}), "application/json")
                return

            if path == "/api/quick-setup":
                radarr_url = str(data.get("radarrUrl", "")).strip()
                radarr_api = str(data.get("radarrApiKey", "")).strip()
                quality_profile_id = safe_int(data.get("qualityProfileId"), 0, minimum=0)
                if not radarr_url or not radarr_api:
                    raise RuntimeError("Radarr URL and API key are required.")
                config["radarr"]["enabled"] = True
                config["radarr"]["url"] = radarr_url
                config["radarr"]["apiKey"] = radarr_api
                service_request(config["radarr"], "GET", "/system/status")
                discovered = discover_radarr_options(config["radarr"])
                if not str(config["radarr"].get("rootFolderPath", "")).strip():
                    if discovered["folders"]:
                        config["radarr"]["rootFolderPath"] = discovered["folders"][0]["path"]
                if quality_profile_id > 0:
                    config["radarr"]["qualityProfileId"] = quality_profile_id
                elif discovered["profiles"]:
                    config["radarr"]["qualityProfileId"] = int(discovered["profiles"][0]["id"])
                config["app"]["setupComplete"] = True
                config["app"]["onboardingDismissed"] = True
                save_config(config)
                self.respond(200, json.dumps({"ok": True, "message": "Setup saved. Dashboard is ready."}), "application/json")
                return

            if path == "/api/onboarding-reset":
                config["app"]["onboardingDismissed"] = False
                save_config(config)
                self.respond(200, json.dumps({"ok": True, "message": "Checklist will be shown again."}), "application/json")
                return

            if path == "/api/logout":
                self.respond(200, json.dumps({"ok": True, "message": "Logged out."}), "application/json", set_cookie="driparr_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                return

            self.respond(404, json.dumps({"ok": False, "message": "Not found."}), "application/json")
        except Exception as error:
            self.respond(500, json.dumps({"ok": False, "message": str(error)}), "application/json")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


def main():
    ensure_data()
    threading.Thread(target=worker_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    print("Driparr webinterface draait op poort 8080.")
    server.serve_forever()


if __name__ == "__main__":
    main()
