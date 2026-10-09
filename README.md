# autorip-transcode-api

Webhook for [freemkv autorip](https://freemkv.org/docs/autorip/). When a rip is moved to its final location, it transcodes the MKV to HEVC with Intel QSV.

## How it works

- `rip_complete` is cached (disc `format`, `year`, keyed by title).
- `move_complete` queues the transcode, using the cached metadata to pick a bitrate ceiling.
- One ffmpeg job runs at a time.

Quality (`-global_quality`) is the same for all discs. Only `maxrate`/`bufsize` change:

| Tier   | maxrate / bufsize |
|--------|-------------------|
| dvd    | 2M / 4M           |
| bluray | 8M / 16M          |
| uhd    | 16M / 32M         |

UHD discs from before 1995, unknown formats and a missing `rip_complete` all use the Blu-ray tier.

## Run

```bash
docker compose up -d --build autorip-transcode-webhook
```

In autorip → Settings, set `webhook_urls` to `http://autorip-transcode-webhook:9000/webhook`.

## Endpoints
There is no auth, so keep it on your LAN or behind a VPN.

- `POST /webhook`: autorip events
- `GET /logs`: logs with ffmpeg progress (`?raw=1`, `?n=1000`). 
- `GET /healthz`

## Config (env vars)

`GLOBAL_QUALITY` (24), `TRANSCODE_OUTPUT_DIR` (/transcoded), `DELETE_SOURCE` (false), `{DVD,BLURAY,UHD}_{MAXRATE,BUFSIZE}`, `UHD_MASTER_YEAR_CUTOFF` (1995), `PROGRESS_INTERVAL_SECS` (60), `LOG_BUFFER_LINES` (2000)

## Test

```bash
curl -X POST localhost:9000/webhook -H 'Content-Type: application/json' \
  -d '{"event":"rip_complete","title":"Test","year":2024,"format":"UHD"}'
curl -X POST localhost:9000/webhook -H 'Content-Type: application/json' \
  -d '{"event":"move_complete","title":"Test","output_path":"/output/input.mkv"}'
```

## Gotchas

- Gen9–11 iGPUs (e.g. Gemini Lake) need the legacy MediaSDK runtime. `vpl-gpu-rt` only supports Gen12 and newer.
- Don't pass `-b:v 0` to QSV. It forces CQP mode.
- `-maxrate` is a soft cap in quality mode.
- Pending metadata is in memory, so a restart between the two events falls back to the Blu-ray tier.
