# Release Checklist (GitHub Public)

## 1) Security & Secrets
- [x] Verify `.env` is **not** committed.
- [x] Verify `config.local.json` is **not** committed.
- [x] Verify no real Radarr API keys in tracked files (`rg -n "apiKey|secret|password|token"`).
- [x] Remove local/reference-only files from Git tracking (`_reference/`, `curl-cookies.txt`).
- [ ] Rotate any key that was previously committed.

## 2) Clean Runtime State
- [x] `data/queue.csv` contains only header row.
- [x] `data-test/queue.csv` contains only header row.
- [x] `data/config.json` contains safe defaults (no private URL/key).
- [x] `data-test/config.json` contains mock-safe defaults.

## 3) Docs & UX
- [x] README reflects IMDb-only flow and current features.
- [x] Portainer compose uses placeholders for password/secret.
- [x] Test login clearly documented for mock environment (`admin/admin`).
- [x] Release notes prepared in `RELEASE_NOTES.md`.

## 4) Functional Smoke Test
- [ ] `docker compose -f docker-compose.test.yml up -d --build`
- [ ] Open `http://localhost:8090`
- [ ] Login works (`admin/admin`)
- [ ] CSV upload works (custom file picker + progress)
- [ ] Queue reasons are human-friendly
- [ ] Sync mode waits for completion before next drip
- [ ] Run history updates

Blocked locally until Docker Desktop is installed/running. Winget download succeeded, but Docker Desktop installer failed at the admin/UAC step with exit code `4294967291`.

## 5) Release Hygiene
- [x] `python -m py_compile scripts/web_app.py scripts/mock_radarr.py`
- [x] `git status` reviewed
- [ ] Commit message is clear and release-oriented
- [x] Tag/release notes prepared

## Suggested First Public Tag
- `v0.1.0`

Suggested release commit:
- `Prepare v0.1.0 public release`
