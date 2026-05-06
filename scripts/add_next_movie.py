import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.local.json"
MOVIES_PATH = ROOT / "movies.csv"


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit("config.local.json ontbreekt. Maak die aan op basis van config.example.json.")

    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    required = [
        "radarrUrl",
        "radarrApiKey",
        "qualityProfileId",
        "rootFolderPath",
        "minimumAvailability",
        "searchForMovie",
        "maxMoviesPerRun",
        "intervalMinutes",
    ]
    missing = [key for key in required if key not in config]
    if missing:
        raise SystemExit(f"Ontbrekende configvelden: {', '.join(missing)}")

    config["radarrUrl"] = config["radarrUrl"].rstrip("/")
    return config


def radarr_request(config, method, path, payload=None):
    data = None
    headers = {
        "X-Api-Key": config["radarrApiKey"],
        "Accept": "application/json",
    }

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        f"{config['radarrUrl']}/api/v3{path}",
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else None
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Radarr HTTP {error.code}: {body}") from error
    except URLError as error:
        raise RuntimeError(f"Kan Radarr niet bereiken: {error.reason}") from error


def read_movies():
    if not MOVIES_PATH.exists():
        raise SystemExit("movies.csv ontbreekt.")

    with MOVIES_PATH.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_movies(rows):
    fieldnames = ["tmdbId", "title", "status", "addedAt", "error"]
    with MOVIES_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_payload(config, movie):
    return {
        "tmdbId": int(movie["tmdbId"]),
        "qualityProfileId": int(config["qualityProfileId"]),
        "rootFolderPath": config["rootFolderPath"],
        "monitored": True,
        "minimumAvailability": config["minimumAvailability"],
        "addOptions": {
            "searchForMovie": bool(config["searchForMovie"]),
        },
    }


def check_radarr(config):
    _, system_status = radarr_request(config, "GET", "/system/status")
    print(f"Radarr bereikbaar: {system_status.get('appName', 'Radarr')} {system_status.get('version', '')}")

    _, profiles = radarr_request(config, "GET", "/qualityprofile")
    print("Quality profiles:")
    for profile in profiles:
        print(f"  {profile['id']}: {profile['name']}")

    _, folders = radarr_request(config, "GET", "/rootfolder")
    print("Root folders:")
    for folder in folders:
        print(f"  {folder['path']}")


def add_next_movies(config, dry_run):
    rows = read_movies()
    todo_rows = [row for row in rows if row.get("status") == "todo"]
    selected = todo_rows[: int(config["maxMoviesPerRun"])]

    if not selected:
        print("Geen films met status todo gevonden.")
        return

    for movie in selected:
        payload = build_payload(config, movie)
        print(f"Volgende film: {movie['title']} (TMDb {movie['tmdbId']})")

        if dry_run:
            print(json.dumps(payload, indent=2))
            continue

        try:
            radarr_request(config, "POST", "/movie", payload)
            movie["status"] = "added"
            movie["addedAt"] = datetime.now(timezone.utc).isoformat()
            movie["error"] = ""
            print("Toegevoegd aan Radarr.")
        except RuntimeError as error:
            movie["status"] = "failed"
            movie["error"] = str(error)
            print(f"Fout: {error}", file=sys.stderr)

    if not dry_run:
        write_movies(rows)


def run_loop(config, dry_run):
    interval_seconds = int(config["intervalMinutes"]) * 60
    print(f"Drip-feed loop gestart. Interval: {config['intervalMinutes']} minuten.")

    while True:
        started_at = datetime.now(timezone.utc).isoformat()
        print(f"\nRun gestart: {started_at}")
        add_next_movies(config, dry_run)
        print(f"Wachten tot volgende run over {config['intervalMinutes']} minuten.")
        time.sleep(interval_seconds)


def main():
    parser = argparse.ArgumentParser(description="Voeg gecontroleerd de volgende film uit movies.csv toe aan Radarr.")
    parser.add_argument("--check", action="store_true", help="Test Radarr en toon quality profiles/root folders.")
    parser.add_argument("--dry-run", action="store_true", help="Toon alleen wat er verzonden zou worden.")
    parser.add_argument("--loop", action="store_true", help="Blijf periodiek draaien volgens intervalMinutes.")
    args = parser.parse_args()

    config = load_config()

    if args.check:
        check_radarr(config)
    elif args.loop:
        run_loop(config, args.dry_run)
    else:
        add_next_movies(config, args.dry_run)


if __name__ == "__main__":
    main()
