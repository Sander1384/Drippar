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
LAST_RUN = {"at": None, "message": "Worker nog niet gestart."}
SESSIONS = {}
SESSION_TTL_SECONDS = 60 * 60 * 12


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def env_admin_username():
    return os.getenv("DRIPARR_ADMIN_USERNAME", "admin")


def env_admin_password():
    return os.getenv("DRIPARR_ADMIN_PASSWORD", "admin")


def env_session_secret():
    return os.getenv("DRIPARR_SESSION_SECRET", "replace-me")


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
            "tmdbImporterEnabled": True,
            "imdbImporterEnabled": False,
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
        app_cfg["tmdbImporterEnabled"] = True
    if "imdbImporterEnabled" not in app_cfg:
        app_cfg["imdbImporterEnabled"] = False
    return config


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
        raise RuntimeError("URL ontbreekt.")

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
        raise RuntimeError(f"Niet bereikbaar: {error.reason}") from error


def tmdb_ids_from_text(text):
    ids = []
    for match in re.findall(r"(?:themoviedb\.org/movie/|tmdb[:=\s]+)(\d+)", text, flags=re.I):
        ids.append(match)
    if text.strip().isdigit():
        ids.append(text.strip())
    return list(dict.fromkeys(ids))


def imdb_ids_from_text(text):
    return list(dict.fromkeys(re.findall(r"tt\d{7,9}", text, flags=re.I)))


def import_list(source_type, url, media_type, name):
    config = read_config()
    if source_type == "tmdb" and not config["app"].get("tmdbImporterEnabled", True):
        raise RuntimeError("TMDb import staat uit. Activeer deze eerst in General settings.")
    if source_type == "imdb" and not config["app"].get("imdbImporterEnabled", False):
        raise RuntimeError("IMDb import staat uit. Activeer deze eerst in General settings.")

    request = Request(url, headers={"User-Agent": "Driparr/1.0"})
    with urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")

    if source_type == "tmdb":
        ids = tmdb_ids_from_text(body + "\n" + url)
        id_kind = "tmdb"
    elif source_type == "imdb":
        ids = imdb_ids_from_text(body + "\n" + url)
        id_kind = "imdb"
    else:
        raise RuntimeError("Onbekend lijsttype.")

    rows = read_queue()
    existing = {(row["type"], row["externalId"]) for row in rows}
    radarr_cfg = config.get("radarr", {})
    can_check_radarr = bool(radarr_cfg.get("url") and radarr_cfg.get("apiKey"))
    added = 0
    for external_id in ids:
        key = (media_type, f"{id_kind}:{external_id}")
        if key in existing:
            continue
        status = "todo"
        reason = ""
        if media_type == "movie" and can_check_radarr:
            try:
                tmdb_for_check = None
                if id_kind == "tmdb":
                    tmdb_for_check = external_id
                elif id_kind == "imdb":
                    tmdb_for_check = resolve_tmdb_from_imdb(config, external_id)
                if tmdb_for_check and radarr_has_tmdb(config, int(tmdb_for_check)):
                    status = "skipped"
                    reason = "Bestaat al in Radarr (gefilterd bij import)."
            except RuntimeError:
                pass
        rows.append(
            {
                "type": media_type,
                "externalId": f"{id_kind}:{external_id}",
                "title": f"{media_type.upper()} {external_id}",
                "status": status,
                "source": name or url,
                "addedAt": "",
                "error": reason,
            }
        )
        existing.add(key)
        added += 1
    write_queue(rows)
    return added


def radarr_payload(config, item):
    kind, value = item["externalId"].split(":", 1)
    if kind == "imdb":
        value = resolve_tmdb_from_imdb(config, value)
        if not value:
            raise RuntimeError(f"IMDb kon niet naar TMDb worden vertaald: {item['externalId']}")
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


def discover_radarr_options(service):
    profiles = service_request(service, "GET", "/qualityprofile")
    folders = service_request(service, "GET", "/rootfolder")
    return {
        "profiles": [{"id": p.get("id"), "name": p.get("name")} for p in (profiles or [])],
        "folders": [{"path": f.get("path")} for f in (folders or [])],
    }


def process_once():
    config = read_config()
    rows = read_queue()
    max_items = int(config["app"].get("maxItemsPerRun", 1))
    selected = [row for row in rows if row["status"] == "todo"][:max_items]
    if not selected:
        LAST_RUN.update({"at": utc_now(), "message": "Geen todo items."})
        return

    for item in selected:
        try:
            if item["type"] == "movie":
                payload = radarr_payload(config, item)
                if radarr_has_tmdb(config, payload["tmdbId"]):
                    item["status"] = "skipped"
                    item["error"] = "Bestaat al in Radarr (duplicate voorkomen)."
                    item["addedAt"] = utc_now()
                    LAST_RUN.update({"at": item["addedAt"], "message": f"Overgeslagen (al aanwezig): {item['title']}"})
                    continue
                service_request(config["radarr"], "POST", "/movie", payload)
            else:
                raise RuntimeError("Series workflow volgt later.")
            item["status"] = "added"
            item["addedAt"] = utc_now()
            item["error"] = ""
            LAST_RUN.update({"at": item["addedAt"], "message": f"Toegevoegd: {item['title']}"})
        except RuntimeError as error:
            item["status"] = "failed"
            item["error"] = str(error)
            LAST_RUN.update({"at": utc_now(), "message": f"Fout bij {item['title']}: {error}"})
    write_queue(rows)


def worker_loop():
    ensure_data()
    while True:
        config = read_config()
        if config["app"].get("workerEnabled"):
            with LOCK:
                process_once()
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
    return f"""<!doctype html>
<html lang=\"nl\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Driparr Login</title>
<style>
:root {{ --bg:#070214; --panel:#141025; --line:#5941a6; --text:#f3efff; --muted:#a596c9; --accent:#7d4bff; --accent2:#5e2eea; --danger:#ff6b8f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; font-family:Segoe UI,Arial,sans-serif; background:radial-gradient(circle at 30% 20%,#1c1143 0,#0c0622 45%,#050212 100%); color:var(--text); }}
.card {{ width:min(520px,92vw); border:1px solid #3c2d73; border-radius:16px; padding:34px; background:linear-gradient(160deg,rgba(255,255,255,.06),rgba(255,255,255,.02)); box-shadow:0 20px 90px rgba(84,46,205,.35); }}
.logo {{ width:72px; height:72px; border-radius:50%; display:grid; place-items:center; margin:0 auto 18px; background:radial-gradient(circle at 30% 30%,#965dff,#4312b9 70%); font-weight:900; }}
h1 {{ text-align:center; margin:0 0 8px; font-size:44px; letter-spacing:.4px; }}
h2 {{ text-align:center; margin:0 0 28px; color:#d3c8f2; font-weight:600; }}
label {{ display:block; margin:14px 0 8px; color:#b9a9e5; font-weight:700; }}
input {{ width:100%; border:1px solid #5e47aa; border-radius:10px; padding:14px 12px; background:#160f2b; color:var(--text); }}
input:focus {{ outline:none; border-color:#8e6bff; box-shadow:0 0 0 3px rgba(125,75,255,.2); }}
button {{ margin-top:20px; width:100%; border:0; border-radius:10px; padding:13px; color:white; background:linear-gradient(140deg,var(--accent),var(--accent2)); font-size:16px; font-weight:800; cursor:pointer; }}
.error {{ margin:10px 0 0; color:var(--danger); font-weight:700; }}
</style></head>
<body><form method=\"POST\" action=\"/login\" class=\"card\"><div class=\"logo\">D</div><h1>Driparr</h1><h2>Sign In</h2><label>Username</label><input name=\"username\" autocomplete=\"username\" required><label>Password</label><input name=\"password\" type=\"password\" autocomplete=\"current-password\" required>{err}<button type=\"submit\">Sign In</button></form></body></html>"""

def setup_page(config, error=""):
    err = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    radarr = config["radarr"]
    return f"""<!doctype html>
<html lang=\"nl\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Driparr Setup</title>
<style>
:root {{ --bg:#070214; --panel:#141025; --line:#5941a6; --text:#f3efff; --muted:#a596c9; --accent:#7d4bff; --accent2:#5e2eea; --danger:#ff6b8f; }}
* {{ box-sizing:border-box; }} body {{ margin:0; min-height:100vh; display:grid; place-items:center; font-family:Segoe UI,Arial,sans-serif; background:radial-gradient(circle at 30% 20%,#1c1143 0,#0c0622 45%,#050212 100%); color:var(--text); }}
.card {{ width:min(780px,94vw); border:1px solid #3c2d73; border-radius:16px; padding:34px; background:linear-gradient(160deg,rgba(255,255,255,.06),rgba(255,255,255,.02)); }}
h1 {{ margin:0 0 6px; font-size:34px; }} p {{ color:var(--muted); margin-top:0; }} label {{ display:block; margin:12px 0 6px; font-weight:700; color:#c4b7ea; }}
input {{ width:100%; border:1px solid #5e47aa; border-radius:10px; padding:12px; background:#160f2b; color:var(--text); }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }} .actions {{ margin-top:18px; display:flex; gap:10px; flex-wrap:wrap; }} button {{ border:0; border-radius:10px; padding:12px 14px; color:white; background:linear-gradient(140deg,var(--accent),var(--accent2)); font-weight:800; cursor:pointer; }}
.btn-secondary {{ background:#21173a; border:1px solid #4e3c7c; }}
.error {{ margin:10px 0 0; color:var(--danger); font-weight:700; }} @media (max-width:840px) {{ .grid {{ grid-template-columns:1fr; }} }}
.logo {{ width:88px; height:88px; margin:0 auto 8px; border-radius:50%; object-fit:cover; border:1px solid #584092; box-shadow:0 12px 45px rgba(120,60,255,.45); display:block; }}
</style></head>
<body><form method=\"POST\" action=\"/setup\" class=\"card\"><img class=\"logo\" src=\"/assets/driparr.png\" alt=\"Driparr\"><h1>Welcome to Driparr</h1><p>Koppel eerst Radarr. Root folder en quality profile kun je automatisch laten ophalen.</p>
<div class=\"grid\">
<div><label>Radarr URL</label><input name=\"radarrUrl\" value=\"{html.escape(radarr.get('url',''))}\" required></div>
<div><label>Radarr API key</label><input name=\"radarrApiKey\" value=\"{html.escape(radarr.get('apiKey',''))}\" required></div>
<div><label>Quality Profile ID</label><input name=\"qualityProfileId\" value=\"{html.escape(str(radarr.get('qualityProfileId',1)))}\" required></div>
<div><label>Root Folder</label><input name=\"rootFolderPath\" value=\"{html.escape(radarr.get('rootFolderPath','/movies'))}\" required></div>
<div><label>TMDb importer (true/false)</label><input name=\"tmdbImporterEnabled\" value=\"true\"></div>
<div><label>IMDb importer (true/false)</label><input name=\"imdbImporterEnabled\" value=\"false\"></div>
</div>{err}<div class=\"actions\"><button class=\"btn-secondary\" type=\"button\" onclick=\"discoverRadarr()\">Auto-detect folders/profiles</button><button type=\"submit\">Finish setup</button></div></form>
<script>
async function discoverRadarr() {{
  const payload = {{
    url: document.querySelector('input[name=\"radarrUrl\"]').value,
    apiKey: document.querySelector('input[name=\"radarrApiKey\"]').value
  }};
  const r = await fetch('/api/radarr/discover', {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify(payload)
  }});
  const j = await r.json();
  if (!j.ok) {{ alert(j.message || 'Discover failed'); return; }}
  if (j.profiles && j.profiles.length) document.querySelector('input[name=\"qualityProfileId\"]').value = j.profiles[0].id;
  if (j.folders && j.folders.length) document.querySelector('input[name=\"rootFolderPath\"]').value = j.folders[0].path;
}}
</script>
</body></html>"""


def settings_form(name, values):
    checked = "checked" if values.get("enabled") else ""
    return f"""<div class="panel"><div class="grid">
<div><label>Enabled</label><input id="{name}Enabled" type="checkbox" {checked}></div>
<div><label>API key</label><input id="{name}ApiKey" value="{html.escape(values.get('apiKey',''))}" placeholder="API key"></div>
<div><label>URL</label><input id="{name}Url" value="{html.escape(values.get('url',''))}" placeholder="http://radarr:7878"></div>
<div><label>Quality Profile ID</label><input id="{name}Quality" type="number" value="{html.escape(str(values.get('qualityProfileId',1)))}"></div>
<div><label>Root Folder</label><input id="{name}Root" value="{html.escape(values.get('rootFolderPath',''))}" placeholder="/movies"></div>
</div><div class="actions"><button class="btn" onclick="saveService('{name}')">Save</button><button class="btn secondary" onclick="testService('{name}')">Test</button></div></div>"""


def page(config, queue):
    todo = [r for r in queue if r.get("status") == "todo"]
    completed = [r for r in queue if r.get("status") == "added"]
    skipped = [r for r in queue if r.get("status") == "skipped"]
    failed = [r for r in queue if r.get("status") == "failed"]
    queue_rows = "\n".join(
        f"<tr><td>{html.escape(row['type'])}</td><td>{html.escape(row['title'])}</td><td>{html.escape(row['externalId'])}</td><td><span class='pill {html.escape(row['status'])}'>{html.escape(row['status'])}</span></td><td>{html.escape(row['source'])}</td><td>{html.escape(row.get('error',''))}</td></tr>"
        for row in queue[-140:]
    )
    todo_rows = "\n".join(
        f"<li><span>{html.escape(r['title'])}</span><small>{html.escape(r['externalId'])}</small></li>"
        for r in todo[:12]
    ) or "<li><span>Geen todo items</span><small>Queue is leeg</small></li>"
    done_rows = "\n".join(
        f"<li><span>{html.escape(r['title'])}</span><small>{html.escape(r.get('addedAt',''))}</small></li>"
        for r in completed[:12]
    ) or "<li><span>Nog niets toegevoegd</span><small>Run worker om te starten</small></li>"
    skipped_rows = "\n".join(
        f"<li><span>{html.escape(r['title'])}</span><small>{html.escape(r.get('error','Already in library'))}</small></li>"
        for r in skipped[:12]
    ) or "<li><span>Geen bestaande films gedetecteerd</span><small>Alles klaar om te drippen</small></li>"
    return f"""<!doctype html><html lang=\"nl\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Driparr</title>
<style>
:root {{ --bg:#090315; --panel:#141026; --line:#3b2d70; --text:#f1ecff; --muted:#9f92c9; --accent:#8a5fff; --green:#2fdd8f; --yellow:#ffbe3d; --red:#ff6f8f; }}
* {{ box-sizing:border-box; }} body {{ margin:0; font-family:Segoe UI,Arial,sans-serif; color:var(--text); background:radial-gradient(circle at 35% 0,#1a1041 0,#09031d 50%,#060214 100%); }}
.app {{ display:grid; grid-template-columns:260px 1fr; min-height:100vh; }} aside {{ background:linear-gradient(180deg,#2f135f,#1f0e45); border-right:1px solid #50358a; padding:14px 10px; }}
.brand {{ display:flex; align-items:center; gap:10px; padding:8px 10px 16px; font-size:28px; font-weight:800; }} .logo-img {{ width:36px; height:36px; border-radius:9px; object-fit:cover; border:1px solid #6c54b7; background:#200f42; }}
nav button {{ width:100%; border:0; text-align:left; color:#d8cfff; background:transparent; border-radius:10px; padding:12px 12px; font-weight:700; cursor:pointer; margin:4px 0; display:flex; align-items:center; gap:9px; }}
nav button.active, nav button:hover {{ background:#4a2494; color:white; }} .nav-icon {{ width:18px; height:18px; stroke:currentColor; fill:none; stroke-width:1.8; }}
main {{ padding:26px; }} h1 {{ margin:0 0 8px; font-size:38px; }} h1 span {{ color:var(--accent); }} .sub {{ color:var(--muted); margin:0 0 18px; }}
.tab {{ display:none; }} .tab.active {{ display:block; }} .panel,.stat {{ background:linear-gradient(160deg,rgba(255,255,255,.07),rgba(255,255,255,.015)); border:1px solid var(--line); border-radius:12px; }}
.panel {{ padding:18px; margin-top:14px; box-shadow:0 10px 36px rgba(0,0,0,.26); }} .statrow {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; margin:14px 0; }}
.stat {{ padding:14px; }} .stat b {{ display:block; margin-top:5px; font-size:24px; }} .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
label {{ display:block; margin:10px 0 6px; font-weight:700; color:#cabdf0; }} input,select {{ width:100%; border:1px solid #4d3a86; border-radius:9px; padding:11px 10px; color:var(--text); background:#120b26; }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }} .btn {{ border:0; border-radius:9px; padding:11px 14px; color:white; background:linear-gradient(130deg,#8f62ff,#6635e8); font-weight:800; cursor:pointer; }}
.btn.secondary {{ background:#26163f; border:1px solid #4a3b67; }} .btn.danger {{ background:#d73b58; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:10px; border-bottom:1px solid #312453; text-align:left; }} th {{ color:#a79ac9; font-size:12px; text-transform:uppercase; }}
.pill {{ display:inline-block; border-radius:7px; padding:3px 8px; font-size:12px; font-weight:700; }} .pill.todo {{ background:#392c11; color:var(--yellow); }} .pill.added {{ background:#123425; color:var(--green); }} .pill.failed {{ background:#3a1320; color:var(--red); }} .pill.skipped {{ background:#2d2742; color:#cbb9ff; }}
.hourglass-wrap {{ display:grid; grid-template-columns:1fr 100px 1fr; gap:12px; align-items:stretch; }} .feed-list {{ background:#100a20; border:1px solid #2f2450; border-radius:12px; padding:12px; }}
.feed-list h3 {{ margin:2px 0 10px; font-size:16px; }} .feed-list ul {{ list-style:none; margin:0; padding:0; max-height:270px; overflow:auto; }} .feed-list li {{ display:flex; justify-content:space-between; gap:10px; padding:7px 6px; border-bottom:1px solid #2a1f44; }}
.feed-list li span {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }} .feed-list li small {{ color:#a49ac2; font-size:12px; }}
.hourglass {{ align-self:stretch; display:flex; align-items:center; justify-content:center; position:relative; }} .hourglass svg {{ width:76px; height:240px; filter:drop-shadow(0 0 18px rgba(138,95,255,.5)); }}
.instance-card {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }} .instance-title {{ font-size:22px; font-weight:800; }} .enabled-pill {{ background:#123c2a; color:#3fe08f; padding:4px 8px; border-radius:7px; font-size:12px; font-weight:700; }} .instance-actions {{ display:flex; gap:8px; }}
.toast {{ position:fixed; right:18px; bottom:18px; background:#22143a; border:1px solid #544078; color:white; border-radius:8px; padding:12px; display:none; }}
@media (max-width:980px) {{ .app {{ grid-template-columns:1fr; }} .grid,.statrow,.hourglass-wrap {{ grid-template-columns:1fr; }} .hourglass {{ display:none; }} }}
</style></head>
<body><div class=\"app\"><aside><div class=\"brand\"><img class=\"logo-img\" src=\"/assets/driparr.png\" alt=\"Driparr\">Driparr</div><nav>
<button class=\"active\" data-tab=\"dashboard\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M3 13h8V3H3v10zm10 8h8V3h-8v18zM3 21h8v-6H3v6z\"/></svg>Dashboard</button>
<button data-tab=\"lists\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01\"/></svg>Lists</button>
<button data-tab=\"queue\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M4 7h16M4 12h16M4 17h10\"/></svg>Queue</button>
<button data-tab=\"radarr\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"m8 5 11 7-11 7V5z\"/></svg>Radarr</button>
<button data-tab=\"sonarr\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M12 3l4.5 2.6v5.2L12 13.4 7.5 10.8V5.6L12 3zM7.5 13.2 12 15.8l4.5-2.6v5.2L12 21l-4.5-2.6v-5.2z\"/></svg>Sonarr</button>
<button data-tab=\"general\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M12 15.5A3.5 3.5 0 1 0 12 8.5a3.5 3.5 0 0 0 0 7z\"/><path d=\"M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.55V21a2 2 0 0 1-4 0v-.08a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.55-1H3a2 2 0 0 1 0-4h.08a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.01a1.7 1.7 0 0 0 1-1.55V3a2 2 0 0 1 4 0v.08a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.01a1.7 1.7 0 0 0 1.55 1H21a2 2 0 0 1 0 4h-.08a1.7 1.7 0 0 0-1.55 1z\"/></svg>General</button>
<button onclick=\"logout()\"><svg class=\"nav-icon\" viewBox=\"0 0 24 24\"><path d=\"M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4\"/><path d=\"m16 17 5-5-5-5\"/><path d=\"M21 12H9\"/></svg>Logout</button>
</nav></aside><main>
<section id=\"dashboard\" class=\"tab active\"><h1>Dripfeed <span>Dashboard</span></h1><p class=\"sub\">Visual queue flow: top = waiting, bottom = completed.</p>
<div class=\"statrow\"><div class=\"stat\">Todo<b>{len(todo)}</b></div><div class=\"stat\">Added<b>{len(completed)}</b></div><div class=\"stat\">Skipped<b>{len(skipped)}</b></div><div class=\"stat\">Failed<b>{len(failed)}</b></div><div class=\"stat\">Worker<b>{'On' if config['app'].get('workerEnabled') else 'Off'}</b></div></div>
<div class=\"panel\"><div class=\"hourglass-wrap\"><div class=\"feed-list\"><h3>Top Chamber: Waiting to drip</h3><ul>{todo_rows}</ul></div><div class=\"hourglass\"><svg viewBox=\"0 0 100 300\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\"><path d=\"M20 15h60c0 60-30 75-30 120 0 45 30 60 30 120H20c0-60 30-75 30-120C50 90 20 75 20 15z\" stroke=\"#a67dff\" stroke-width=\"2\"/><path d=\"M33 29h34c-2 22-10 36-17 48-7-12-15-26-17-48z\" fill=\"#7b46ff\"/><path d=\"M45 146h10v14h-10z\" fill=\"#bfa2ff\"/><path d=\"M33 270h34c-3-20-11-34-17-45-6 11-14 25-17 45z\" fill=\"#7b46ff\"/></svg></div><div class=\"feed-list\"><h3>Bottom Chamber: Dripped / Completed</h3><ul>{done_rows}</ul></div></div><div class=\"actions\"><button class=\"btn\" onclick=\"runNow()\">Run now</button><button class=\"btn secondary\" onclick=\"saveGeneral(true)\">Toggle worker</button></div></div>
<div class=\"panel\"><h3 style=\"margin-top:0\">Already in Radarr (Auto-filtered)</h3><p class=\"sub\">Deze films worden niet gedownload omdat ze al in de library staan.</p><div class=\"feed-list\"><ul>{skipped_rows}</ul></div></div>
</section>
<section id=\"lists\" class=\"tab\"><h1>Lists</h1><p class=\"sub\">Importeer IMDb/TMDb links in je wachtrij.</p><div class=\"panel\"><div class=\"grid\"><div><label>Naam</label><input id=\"listName\"></div><div><label>Type</label><select id=\"listType\"><option value=\"tmdb\">TMDb</option><option value=\"imdb\">IMDb</option></select></div><div><label>Media</label><select id=\"listMedia\"><option value=\"movie\">Movies</option><option value=\"series\">Series</option></select></div><div><label>URL</label><input id=\"listUrl\"></div></div><div class=\"actions\"><button class=\"btn\" onclick=\"addList()\">Add and import</button></div></div></section>
<section id=\"queue\" class=\"tab\"><h1>Queue</h1><div class=\"panel\"><table><thead><tr><th>Type</th><th>Title</th><th>ID</th><th>Status</th><th>Source</th><th>Reason</th></tr></thead><tbody>{queue_rows}</tbody></table></div></section>
<section id=\"radarr\" class=\"tab\"><h1>Radarr <span>Settings</span></h1><p class=\"sub\">Manage your Radarr instance.</p><div class=\"panel\"><div class=\"instance-card\"><div><div class=\"instance-title\">Instances</div><div style=\"margin-top:10px\"><b>Radarr</b> <span class=\"enabled-pill\">Enabled</span> <span style=\"color:#9f92c9;margin-left:8px\">{html.escape(config['radarr'].get('url',''))}</span></div></div><div class=\"instance-actions\"><button class=\"btn\" onclick=\"saveService('radarr')\">Save</button><button class=\"btn secondary\" onclick=\"testService('radarr')\">Test</button></div></div>{settings_form('radarr', config['radarr'])}</div></section>
<section id=\"sonarr\" class=\"tab\"><h1>Sonarr</h1>{settings_form('sonarr', config['sonarr'])}</section>
<section id=\"general\" class=\"tab\"><h1>General</h1><div class=\"panel\"><div class=\"grid\"><div><label>Drip interval</label><select id=\"intervalPreset\" onchange=\"setIntervalFromPreset()\"><option value=\"15\">Elke 15 minuten</option><option value=\"30\">Elke 30 minuten</option><option value=\"60\">Elk uur</option><option value=\"90\">Elke 1,5 uur</option><option value=\"custom\">Custom</option></select></div><div><label>Interval minuten (custom)</label><input id=\"intervalMinutes\" type=\"number\" value=\"{config['app'].get('intervalMinutes')}\"></div><div><label>Max items per run</label><input id=\"maxItemsPerRun\" type=\"number\" value=\"{config['app'].get('maxItemsPerRun')}\"></div><div><label>TMDb import actief</label><input id=\"tmdbImporterEnabled\" type=\"checkbox\" {'checked' if config['app'].get('tmdbImporterEnabled', True) else ''}></div><div><label>IMDb import actief</label><input id=\"imdbImporterEnabled\" type=\"checkbox\" {'checked' if config['app'].get('imdbImporterEnabled', False) else ''}></div></div><div class=\"actions\"><button class=\"btn\" onclick=\"saveGeneral()\">Save</button></div></div></section>
</main></div><div id=\"toast\" class=\"toast\"></div>
<script>
document.querySelectorAll('nav button[data-tab]').forEach(btn => btn.onclick = () => {{ document.querySelectorAll('nav button').forEach(b => b.classList.remove('active')); document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); btn.classList.add('active'); document.getElementById(btn.dataset.tab).classList.add('active'); }});
async function post(url, data) {{ const r = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data)}}); if (r.status===401) {{ location.href='/login'; return; }} const j = await r.json(); toast(j.message || (j.ok ? 'Saved' : 'Error')); if (j.reload) setTimeout(()=>location.reload(), 650); return j; }}
function toast(msg) {{ const t=document.getElementById('toast'); t.textContent=msg; t.style.display='block'; setTimeout(()=>t.style.display='none',3000); }}
function serviceData(name) {{ return {{url:document.getElementById(name+'Url').value, apiKey:document.getElementById(name+'ApiKey').value, qualityProfileId:Number(document.getElementById(name+'Quality').value), rootFolderPath:document.getElementById(name+'Root').value, enabled:document.getElementById(name+'Enabled').checked}}; }}
function saveService(name) {{ post('/api/service/'+name, serviceData(name)); }}
function testService(name) {{ post('/api/service/'+name+'/test', serviceData(name)); }}
function saveGeneral(toggle) {{ post('/api/general', {{intervalMinutes:Number(intervalMinutes.value), maxItemsPerRun:Number(maxItemsPerRun.value), tmdbImporterEnabled:tmdbImporterEnabled.checked, imdbImporterEnabled:imdbImporterEnabled.checked, toggleWorker:!!toggle}}); }}
function setIntervalFromPreset() {{ if (intervalPreset.value !== 'custom') intervalMinutes.value = Number(intervalPreset.value); }}
(() => {{ const v = Number(intervalMinutes.value); if ([15,30,60,90].includes(v)) intervalPreset.value = String(v); else intervalPreset.value = 'custom'; }})();
function addList() {{ post('/api/lists', {{name:listName.value, type:listType.value, mediaType:listMedia.value, url:listUrl.value}}); }}
function runNow() {{ post('/api/run-now', {{}}); }}
async function logout() {{ await post('/api/logout', {{}}); location.href='/login'; }}
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
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
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
        if path == "/assets/driparr.png":
            image_path = ASSETS / "driparr.png"
            if image_path.exists():
                payload = image_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.respond(404, "Not found", "text/plain")
            return
        if path == "/login":
            self.respond(200, login_page())
            return
        if self.auth_required():
            return
        config = read_config()
        if not config["app"].get("setupComplete") and path != "/setup":
            self.respond(302, "", location="/setup")
            return
        if path == "/setup":
            self.respond(200, setup_page(config))
            return
        queue = read_queue()
        self.respond(200, page(config, queue))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            form = self.read_form()
            username = form.get("username", "")
            password = form.get("password", "")
            if hmac.compare_digest(username, env_admin_username()) and hmac.compare_digest(password, env_admin_password()):
                session_id = issue_session(username)
                cookie = f"driparr_session={session_id}; HttpOnly; Path=/; SameSite=Lax"
                self.respond(302, "", set_cookie=cookie, location="/")
            else:
                self.respond(200, login_page("Ongeldige gebruikersnaam of wachtwoord."))
            return
        if path == "/setup":
            try:
                form = self.read_form()
                config = read_config()
                config["radarr"]["url"] = form.get("radarrUrl", "").strip()
                config["radarr"]["apiKey"] = form.get("radarrApiKey", "").strip()
                config["radarr"]["qualityProfileId"] = int(form.get("qualityProfileId", "1"))
                root_path = form.get("rootFolderPath", "").strip()
                if not root_path:
                    discovered = discover_radarr_options(config["radarr"])
                    if discovered["folders"]:
                        root_path = discovered["folders"][0]["path"]
                if not root_path:
                    raise RuntimeError("Root folder is vereist. Kies een geldige map uit Radarr.")
                config["radarr"]["rootFolderPath"] = root_path
                config["app"]["tmdbImporterEnabled"] = form.get("tmdbImporterEnabled", "true").strip().lower() == "true"
                config["app"]["imdbImporterEnabled"] = form.get("imdbImporterEnabled", "false").strip().lower() == "true"
                config["app"]["setupComplete"] = True
                save_config(config)
                self.respond(302, "", location="/")
            except Exception as error:
                self.respond(200, setup_page(read_config(), str(error)))
            return

        if path != "/login" and self.auth_required():
            return

        try:
            data = self.read_json()
            config = read_config()

            if path.startswith("/api/service/") and path.endswith("/test"):
                name = path.split("/")[3]
                service_request(data, "GET", "/system/status")
                self.respond(200, json.dumps({"ok": True, "message": f"{name.title()} bereikbaar."}), "application/json")
                return

            if path == "/api/radarr/discover":
                service = {"url": data.get("url", ""), "apiKey": data.get("apiKey", "")}
                found = discover_radarr_options(service)
                self.respond(200, json.dumps({"ok": True, **found}), "application/json")
                return

            if path.startswith("/api/service/"):
                name = path.split("/")[3]
                config[name].update(data)
                if name == "radarr" and not str(config["radarr"].get("rootFolderPath", "")).strip():
                    discovered = discover_radarr_options(config["radarr"])
                    if discovered["folders"]:
                        config["radarr"]["rootFolderPath"] = discovered["folders"][0]["path"]
                save_config(config)
                self.respond(200, json.dumps({"ok": True, "message": f"{name.title()} opgeslagen.", "reload": True}), "application/json")
                return

            if path == "/api/general":
                config["app"]["intervalMinutes"] = int(data["intervalMinutes"])
                config["app"]["maxItemsPerRun"] = int(data["maxItemsPerRun"])
                config["app"]["tmdbImporterEnabled"] = bool(data.get("tmdbImporterEnabled", True))
                config["app"]["imdbImporterEnabled"] = bool(data.get("imdbImporterEnabled", False))
                if data.get("toggleWorker"):
                    config["app"]["workerEnabled"] = not config["app"].get("workerEnabled")
                save_config(config)
                self.respond(200, json.dumps({"ok": True, "message": "Instellingen opgeslagen.", "reload": True}), "application/json")
                return

            if path == "/api/lists":
                config["lists"].append(data)
                save_config(config)
                added = import_list(data["type"], data["url"], data["mediaType"], data.get("name", ""))
                self.respond(200, json.dumps({"ok": True, "message": f"{added} items geimporteerd.", "reload": True}), "application/json")
                return

            if path == "/api/run-now":
                with LOCK:
                    process_once()
                self.respond(200, json.dumps({"ok": True, "message": LAST_RUN["message"], "reload": True}), "application/json")
                return

            if path == "/api/logout":
                self.respond(200, json.dumps({"ok": True, "message": "Uitgelogd."}), "application/json", set_cookie="driparr_session=; Path=/; Max-Age=0")
                return

            self.respond(404, json.dumps({"ok": False, "message": "Niet gevonden."}), "application/json")
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
