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

Create a data folder and a `docker-compose.yml`:

```yaml
services:
  driparr:
    image: ghcr.io/sander1384/seerrdripfeed:v0.1.0
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

- Do not publish real Radarr API keys, production `.env` files, or local config files.
- Rotate any API keys or passwords that were used during early testing.
