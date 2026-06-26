# Driparr v0.2.3

- Automatically releases a sync-mode movie when Radarr only has an old grabbed-history record and no live download/import queue entry for ten minutes.
- Prevents orphaned qBittorrent/Radarr downloads from blocking every later drip indefinitely.

# Driparr v0.2.2

- Adds a per-list movie storage picker backed by Radarr's accessible root folders.
- Stores the selected root folder on every queued movie, so films added from separate lists keep their intended destination.
- Adds a safe SQLite migration for the new queue field and validates every selected destination against Radarr.

# Driparr v0.2.1

- Fixes IMDb CSV upload after CSRF hardening by sending the session-bound CSRF token with the upload request.
- Adds a regression test so the CSV upload cannot silently bypass the shared POST helper again.

# Driparr v0.2.0

Reliability foundation release for Driparr.

## Highlights

- Added transactional SQLite storage with WAL and automatic JSON/CSV migration.
- Kept atomic JSON/CSV compatibility exports for rollback safety.
- Added daily SQLite backups with configurable retention.
- Added an explicit validated queue state machine.
- Cached TMDb and Radarr movie IDs per queue item to eliminate repeated IMDb lookups.
- Added bounded retries, exponential backoff, and per-item retry metadata.
- Added authenticated worker-health diagnostics and an unauthenticated Docker health endpoint.
- Added Docker healthcheck support and visible runtime health in the dashboard.
- Added CSRF protection, login rate limiting, security headers, secure-cookie support, and fail-closed default-secret checks.
- Reduced noisy dashboard polling logs and switched request logs to structured JSON.
- Split transactional storage and state transitions into focused modules.
- Expanded the test suite with migration, storage, state-machine, retry, security, and mock-Radarr lifecycle coverage.

Existing data migrates automatically on first start. The original files are backed up before migration.

---

# Driparr v0.1.19

Worker resilience release for Driparr.

## Reliability fix

- Radarr socket timeouts are converted to recoverable service errors.
- The automatic worker now catches unexpected iteration failures, records the error, and retries automatically.
- A successful retry records that the worker recovered instead of silently remaining stalled.
- Added regression tests for Radarr timeouts and worker recovery.

This fixes an issue where a temporary timeout during IMDb-to-TMDb lookup could terminate the background worker while the web interface continued to appear healthy.

---

# Driparr v0.1.18

Dashboard clarity release for Driparr.

This release removes the weak Recent Events dashboard panel and puts the more useful Already in Library list in its place.

It keeps the public beta README, issue templates, Radarr status handling, live feedback, language consistency, and sync-mode behavior from the previous release.

## Highlights

- Replaced the Recent Events dashboard panel with Already in Library.
- The duplicate-skip list is now higher on the dashboard.
- README now routes new users to the safe mock Radarr test stack before real Radarr setup.
- Added clearer "Looking for testers" and feedback instructions.
- Added a "What Driparr is not" section to set expectations before promotion.
- Added GitHub issue templates for bugs, installation help, feature requests, and tester feedback.
- Added ready-to-use public beta promotion posts.
- IMDb CSV import for movie and series exports.
- Radarr onboarding with connection test, quality profile selection, and root folder setup.
- Queue dashboard with readable status and reason display.
- Duplicate protection: existing Radarr movies are skipped instead of re-added.
- Timed drip mode with configurable interval and max items per run.
- Sync mode waits only for Driparr's own active queue item, not unrelated Radarr downloads.
- Radarr red/warning/failed/stalled/import-blocked queue entries are treated as failed downloads so items can be skipped instead of waiting forever.
- No-release and unresolvable IMDb cases now show clearer queue reasons and liveblog messages.
- The liveblog now uses more varied phrasing instead of repeating first-person "I am..." style messages.
- Spontaneous liveblog messages have their own timer, so normal worker updates no longer suppress them indefinitely.
- The visual Drip Timeline and queue refresh automatically without requiring F5.
- Live updates preserve scroll position.
- Dutch, English, and German UI/language handling has been tightened across static UI, liveblog, queue reasons, onboarding, and service settings.
- Rabbit mood hover text follows the selected/forced language from the initial HTML render.
- Run history and optional webhook notifications.
- Docker Compose and Portainer-ready deployment examples.
- Mock Radarr test stack for browser testing without a real Radarr instance.

## Install

Create a data folder and a `docker-compose.yml`:

```yaml
services:
  driparr:
    image: ghcr.io/sander1384/driparr:v0.1.18
    container_name: driparr
    restart: unless-stopped
    ports:
      - "18080:8080"
    environment:
      TZ: Europe/Amsterdam
      DRIPARR_ADMIN_USERNAME: admin
      DRIPARR_ADMIN_PASSWORD: "CHANGE_ME_STRONG_PASSWORD"
      DRIPARR_SESSION_SECRET: "CHANGE_ME_RANDOM_SECRET"
    volumes:
      - ./data:/app/data
```

Start:

```bash
docker compose up -d
```

Open `http://localhost:18080`, log in, connect Radarr, and import an IMDb CSV from the Lists page.

For Portainer/NAS usage, use `docker-compose.portainer.yml` and set at least:

- `DRIPARR_ADMIN_PASSWORD`
- `DRIPARR_SESSION_SECRET`
- the host volume path for `/app/data`

## Notes

- Downloads added directly in Radarr or by another app no longer block Driparr.
- Do not publish real Radarr API keys, production `.env` files, or local config files.
- Rotate any API keys or passwords that were used during early testing.
