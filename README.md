# Driparr

Driparr voert films gecontroleerd (drip-feed) naar Radarr in plaats van bulk-imports tegelijk.

## Features

- Loginpagina met username/password
- Setup wizard na eerste login
- Radarr setup via UI (URL, API key, profile, root folder)
- IMDb CSV list import (movie/series)
- Duplicate filter: films die al in Radarr staan worden automatisch overgeslagen (`skipped`)
- Drip-feed worker (`maxItemsPerRun` per interval)
- Sync mode: wacht op afronding voordat volgende drip start
- Queue voortgang en run history in UI
- Optionele webhook notificaties
- Docker Compose setup voor hergebruik

## Productie starten

1. Kopieer env-template:

```bash
cp .env.example .env
```

2. Zet sterke waarden in `.env`.

3. Start:

```bash
docker compose up -d --build
```

4. Open `http://localhost:${DRIPARR_PORT}` (default `18080` in Portainer stack).

## Portainer / NAS Stack

Gebruik voor Portainer een image-based stack (geen lokale build op je pc).

1. Push deze repo naar GitHub onder `Sander1384`.
2. GitHub Action bouwt en pusht automatisch image naar:
`ghcr.io/sander1384/seerrdripfeed:latest`
3. Zet package visibility van het GHCR image op `public` (GitHub -> Packages).
4. Open [docker-compose.portainer.yml](./docker-compose.portainer.yml).
5. Pas alleen nog aan:
- `DRIPARR_ADMIN_PASSWORD`
- `DRIPARR_SESSION_SECRET`
- volume pad `/volume1/docker/driparr/data:/app/data`
6. Plak de inhoud in Portainer bij `Stacks` -> `Add stack` -> `Web editor`.
7. Deploy stack en open `http://<NAS-IP>:18080` (of jouw `DRIPARR_PORT`).

## Browser testomgeving (zonder echte Radarr)

Deze stack gebruikt een mock Radarr en draait Driparr op poort `8090`.

Start:

```bash
docker compose -f docker-compose.test.yml up -d --build
```

Open:

```text
http://localhost:8090
```

Login testaccount:

- username: `admin`
- password: `admin`

Stoppen:

```bash
docker compose -f docker-compose.test.yml down
```

## Belangrijk

- Commit nooit echte API keys of productie-wachtwoorden.
- Roteer je Radarr API key als die eerder in plaintext stond.
