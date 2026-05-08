# Release Checklist (GitHub Public)

## 1) Security & Secrets
- [ ] Verify `.env` is **not** committed.
- [ ] Verify `config.local.json` is **not** committed.
- [ ] Verify no real Radarr API keys in tracked files (`rg -n "apiKey|secret|password|token"`).
- [ ] Rotate any key that was previously committed.

## 2) Clean Runtime State
- [ ] `data/queue.csv` contains only header row.
- [ ] `data-test/queue.csv` contains only header row.
- [ ] `data/config.json` contains safe defaults (no private URL/key).
- [ ] `data-test/config.json` contains mock-safe defaults.

## 3) Docs & UX
- [ ] README reflects IMDb-only flow and current features.
- [ ] Portainer compose uses placeholders for password/secret.
- [ ] Test login clearly documented for mock environment (`admin/admin`).

## 4) Functional Smoke Test
- [ ] `docker compose -f docker-compose.test.yml up -d --build`
- [ ] Open `http://localhost:8090`
- [ ] Login works (`admin/admin`)
- [ ] CSV upload works (custom file picker + progress)
- [ ] Queue reasons are human-friendly
- [ ] Sync mode waits for completion before next drip
- [ ] Run history updates

## 5) Release Hygiene
- [ ] `python -m py_compile scripts/web_app.py scripts/mock_radarr.py`
- [ ] `git status` reviewed
- [ ] Commit message is clear and release-oriented
- [ ] Tag/release notes prepared

## Suggested First Public Tag
- `v0.1.0`
