# Driparr v0.1.0

First public release of Driparr: a lightweight drip-feed helper for sending IMDb CSV lists to Radarr in controlled batches.

## Highlights

- IMDb CSV import for movie and series exports.
- Radarr onboarding with connection test, quality profile selection, and root folder setup.
- Queue dashboard with human-readable status and reason display.
- Duplicate protection: existing Radarr movies are skipped instead of re-added.
- Drip-feed worker with configurable interval and max items per run.
- Sync mode that waits for active downloads to complete before adding the next item.
- Run history and optional webhook notifications.
- Docker Compose and Portainer-ready deployment examples.
- Mock Radarr test stack for browser testing without a real Radarr instance.

## Install

For local Docker Compose usage, copy `.env.example` to `.env`, set strong credentials, and run:

```bash
docker compose up -d --build
```

For Portainer/NAS usage, use `docker-compose.portainer.yml` and set at least:

- `DRIPARR_ADMIN_PASSWORD`
- `DRIPARR_SESSION_SECRET`
- the host volume path for `/app/data`

## Notes

- Do not publish real Radarr API keys, production `.env` files, or local config files.
- Rotate any API keys or passwords that were used during early testing.
