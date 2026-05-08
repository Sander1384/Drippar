import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MOVIES = []
DOWNLOADS = []


def utc_iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def refresh_downloads():
    now = time.time()
    keep = []
    for d in DOWNLOADS:
        movie = next((m for m in MOVIES if int(m.get("id", 0)) == int(d.get("movieId", 0))), None)
        if not movie:
            continue
        total = max(1, int(d.get("size", 1)))
        start = float(d.get("startedAt", now))
        duration = max(1.0, float(d.get("durationSec", 60.0)))
        elapsed = max(0.0, now - start)
        ratio = min(1.0, elapsed / duration)
        left = int(max(0, total * (1.0 - ratio)))
        if ratio >= 1.0:
            movie["hasFile"] = True
            movie["movieFile"] = {"quality": "Mock", "size": total}
            continue
        d["sizeleft"] = left
        eta_ts = now + (duration - elapsed)
        d["estimatedCompletionTime"] = utc_iso(eta_ts)
        d["timeleft"] = d["estimatedCompletionTime"]
        d["status"] = "downloading"
        d["trackedDownloadState"] = "downloading"
        keep.append(d)
    DOWNLOADS[:] = keep


def json_response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/api/v3/system/status":
            json_response(self, 200, {"appName": "MockRadarr", "version": "1.0"})
            return
        if path.path == "/api/v3/qualityprofile":
            json_response(self, 200, [{"id": 1, "name": "HD-1080p"}, {"id": 8, "name": "Any"}])
            return
        if path.path == "/api/v3/rootfolder":
            json_response(self, 200, [{"path": "/movies"}])
            return
        if path.path == "/api/v3/movie":
            refresh_downloads()
            json_response(self, 200, MOVIES)
            return
        if path.path == "/api/v3/queue":
            refresh_downloads()
            json_response(self, 200, DOWNLOADS)
            return
        if path.path == "/api/v3/movie/lookup":
            q = parse_qs(path.query)
            term = (q.get("term") or [""])[0]
            if term.startswith("imdb:"):
                imdb = term.split(":", 1)[1]
                lookup = {
                    "tt0137523": 550,
                    "tt0468569": 155,
                    "tt1375666": 27205,
                }
                tmdb = lookup.get(imdb)
                if tmdb:
                    json_response(self, 200, [{"title": imdb, "tmdbId": tmdb}])
                else:
                    json_response(self, 200, [])
                return
            json_response(self, 200, [])
            return
        json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/v3/movie":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
            tmdb_id = int(payload.get("tmdbId", 0))
            for movie in MOVIES:
                if int(movie.get("tmdbId", 0)) == tmdb_id:
                    json_response(self, 409, {"message": "Already exists"})
                    return
            movie_id = len(MOVIES) + 1
            MOVIES.append(
                {
                    "id": movie_id,
                    "title": f"Movie {tmdb_id}",
                    "tmdbId": tmdb_id,
                    "hasFile": False,
                    "movieFile": None,
                }
            )
            DOWNLOADS.append(
                {
                    "id": movie_id,
                    "movieId": movie_id,
                    "title": f"Movie {tmdb_id}",
                    "size": 1_000_000_000,
                    "sizeleft": 1_000_000_000,
                    "estimatedCompletionTime": utc_iso(time.time() + 90),
                    "timeleft": utc_iso(time.time() + 90),
                    "status": "downloading",
                    "trackedDownloadState": "downloading",
                    "startedAt": time.time(),
                    "durationSec": 90.0,
                }
            )
            json_response(self, 201, MOVIES[-1])
            return
        json_response(self, 404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print(fmt % args)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8787), Handler)
    print("Mock Radarr running on 8787")
    server.serve_forever()
