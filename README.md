# Driparr

Driparr sends IMDb lists to Radarr in small, controlled batches instead of flooding Radarr with a full list at once.

It is meant for people who maintain large IMDb watchlists and want Radarr to add movies gradually, with a visible queue, duplicate protection, and an optional sync mode that waits for active downloads to finish before adding more.

## Features

- IMDb CSV import for movie and series exports.
- Radarr setup from the web UI: URL, API key, quality profile, and root folder.
- First-run onboarding checklist.
- Duplicate filter: movies already known by Radarr are skipped automatically.
- Drip-feed worker with configurable interval and max items per run.
- Sync mode: wait for completion before dripping the next item.
- Queue overview with readable status and reasons.
- Run history for imported lists.
- Optional webhook notifications.
- Docker Compose and Portainer-friendly deployment.

## Requirements

- Radarr with API access enabled.
- Docker Compose or Portainer.
- An IMDb CSV export.

To export your IMDb list, open the list on IMDb and use the built-in export option. Driparr reads IMDb IDs from the CSV.

## Quick Start

Create a folder for Driparr data:

```bash
mkdir -p ./driparr/data
cd ./driparr
```

Create `docker-compose.yml`:

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

Start Driparr:

```bash
docker compose up -d
```

Open:

```text
http://localhost:18080
```

Before first use, change `DRIPARR_ADMIN_PASSWORD` and `DRIPARR_SESSION_SECRET` to strong unique values.

## Portainer / NAS

Use [docker-compose.portainer.yml](./docker-compose.portainer.yml) as a starting point.

Change at least:

- `DRIPARR_ADMIN_PASSWORD`
- `DRIPARR_SESSION_SECRET`
- the volume path, for example `/volume1/docker/driparr/data:/app/data`

Then paste the compose file into Portainer:

```text
Stacks -> Add stack -> Web editor
```

Deploy the stack and open:

```text
http://<NAS-IP>:18080
```

## First Setup

1. Log in with the admin username and password from your compose environment.
2. Enter your Radarr URL, for example `http://radarr:7878` or `http://192.168.1.50:7878`.
3. Paste your Radarr API key.
4. Click `Test` to fetch Radarr options.
5. Choose the Radarr quality profile and root folder.
6. Import an IMDb CSV from the `Lists` page.
7. Enable the worker when you are ready for Driparr to start adding items.

## Drip Modes

- `Timed`: add up to `maxItemsPerRun` items every interval.
- `Sync`: add items only when the current Radarr download appears complete.

Sync mode is useful when you want a true one-at-a-time flow. Timed mode is useful when you want predictable batches.

## Test Without Real Radarr

This repository includes a mock Radarr test stack.

```bash
docker compose -f docker-compose.test.yml up -d --build
```

Open:

```text
http://localhost:8090
```

Test login:

- username: `admin`
- password: `admin`

Stop the test stack:

```bash
docker compose -f docker-compose.test.yml down
```

## Updating

For the stable release tag, update the image tag in your compose file when a new release is available.

For automatic latest builds from `main`, use:

```yaml
image: ghcr.io/sander1384/seerrdripfeed:latest
```

Then pull and restart:

```bash
docker compose pull
docker compose up -d
```

## Troubleshooting

If Radarr test fails:

- Check that Driparr can reach the Radarr URL from inside Docker.
- Use the container name, such as `http://radarr:7878`, when both apps are on the same Docker network.
- Use the NAS or server IP when Radarr runs outside the Driparr stack.
- Verify the Radarr API key.

If no items are added:

- Confirm the queue contains `todo` items.
- Confirm the worker is enabled.
- Check whether items were skipped because they already exist in Radarr.
- In sync mode, check whether Radarr still has an active download.

If login stops working after changing secrets:

- Restart the container after changing `DRIPARR_SESSION_SECRET`.
- Make sure the admin password in your compose file is the value you expect.

## Security Notes

- Do not publish real Radarr API keys.
- Do not commit `.env`, local config files, cookies, or data volumes.
- Rotate keys or passwords that were used during testing before opening the app to other users.

## Release

Current public release:

- [Driparr v0.1.0](https://github.com/Sander1384/Drippar/releases/tag/v0.1.0)

Container image:

```text
ghcr.io/sander1384/seerrdripfeed:v0.1.0
```
