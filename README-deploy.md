# Sadpanda Docker Deployment (Server)

This deployment runs one container for Sadpanda (Streamlit + worker) and connects to your existing qBittorrent instance over LAN IP.

## Files in this package

- `deploy/docker/docker-compose.yml`
- `deploy/docker/Dockerfile`
- `deploy/docker/config.template.json`
- `.dockerignore`

## Prerequisites

- Docker + Docker Compose on the server
- qBittorrent already running and reachable at `<server-lan-ip>:8080`
- qBittorrent has `/mnt/seagate4tb` mounted and writable

## Host folder setup

Create required folders once:

```bash
mkdir -p /mnt/seagate4tb/data/torrents/sadpanda
mkdir -p /mnt/seagate4tb/data/media/sadpanda_media
mkdir -p /mnt/seagate4tb/data/media/sadpanda_archives/staging
mkdir -p /mnt/seagate4tb/data/media/sadpanda_archives/generated
```

Check write access for your Docker runtime user:

```bash
touch /mnt/seagate4tb/data/torrents/sadpanda/.write_test
rm /mnt/seagate4tb/data/torrents/sadpanda/.write_test
```

## Configure

1. Copy template:

```bash
cp deploy/docker/config.template.json config.json
```

2. Edit `config.json`:
   - Set your ExHentai cookies.
   - Set `qbittorrent.host` to LAN IP only (example: `192.168.1.50`).
   - Do not include `http://` in `qbittorrent.host`.

## Run

From `deploy/docker`:

```bash
docker compose up -d --build
```

## Verify

- Open UI: `http://<server-ip>:8501`
- Check container logs:

```bash
docker compose logs -f sadpanda
```

You should see successful qB connection.

## Troubleshooting

- **Cannot connect to qBittorrent**
  - Confirm WebUI port is reachable: `curl http://<qb-lan-ip>:8080`
  - Confirm credentials in `config.json`.

- **Missing file/path errors during imports**
  - Confirm both Sadpanda and qB can access the same physical path under `/mnt/seagate4tb`.
  - Confirm `qbittorrent.download_path` exists and is writable.

- **Hardlinks not working**
  - Ensure source and destination are on the same filesystem under `/mnt/seagate4tb`.

## Stop / update

From `deploy/docker`:

```bash
docker compose down
docker compose up -d --build
```
