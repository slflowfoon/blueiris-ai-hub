<p align="center">
  <img src="app/static/logo-mark.svg" alt="Blue Iris AI Hub logo" width="156">
</p>

# Blue Iris AI Hub

AI-powered motion alert processor for [Blue Iris](https://blueirissoftware.com/). When a camera triggers, it analyses the image with Gemini AI, sends a Telegram notification with a caption, and optionally fetches and sends the full video clip.

## Features

- **AI vision analysis** — Gemini 2.5 Flash with automatic API key rotation and fallback to Grok / Groq
- **Video clips** — exports the alert clip from Blue Iris, analyses it with Gemini, and replaces the still photo in Telegram with the video
- **Telegram notifications** — AI-generated captions sent with each alert; captions are updated in-place when video analysis completes
- **Instant notify** — optional per-camera mode that sends the photo immediately with a fallback caption, then updates it once AI analysis completes (guarantees delivery even when Gemini is slow)
- **Auto-mute** — silences a camera automatically after 5 triggers in 10 minutes (prevents spam)
- **Caption modes** — switch to `hilarious`, `witty`, or `rude` captions via Telegram bot commands
- **Auto-mute policy in UI** — tune the trigger threshold, detection window, and mute duration used to suppress noisy cameras
- **Known plates** — teach the AI to recognise and label your vehicles by number plate
- **DVLA enrichment** — any UK number plate detected in a caption is automatically looked up against the DVLA API and annotated with make, colour, year, and tax/MOT status
- **Plate audit log** — every plate lookup is recorded with full DVLA details and a thumbnail of the alert image, viewable in the web UI
- **Web UI** — configure cameras, view logs, manage mutes and plates at `http://your-host:5000`
- **Update notifications** — the UI checks GitHub for new releases and shows a banner when one is available

## Quick Start

### Requirements

- Docker and Docker Compose
- A [Gemini API key](https://aistudio.google.com/) (free tier works)
- A Telegram bot token and chat ID
- Blue Iris running on Windows with `curl.exe` available

### 1. Download and start

```bash
mkdir blueiris-ai-hub && cd blueiris-ai-hub
curl -O https://raw.githubusercontent.com/slflowfoon/blueiris-ai-hub/main/docker-compose.yml
docker compose up -d
```

The web UI will be available at `http://your-host:5000`.

Redis now stores durable BI export job state, session reuse data, and queue coordination. The default Compose file persists this in the named Docker volume `redis_data`, so do not remove that volume unless you intentionally want to discard in-flight BI pipeline state.

Containers now run as a dedicated non-root user (`uid 1000`). If you bind-mount host directories such as `./data` or `./logs`, ensure they are writable by that user on the host.

The default Compose file also configures Docker healthchecks:

- `web` probes `http://localhost:5000/health`
- `redis` uses `redis-cli ping`
- background services use heartbeat files under `/app/data/health`

### 2. Add a camera configuration

Open the web UI and click **+ New Configuration**. Fill in:

| Field | Description |
|-------|-------------|
| Name | Camera name (e.g. `Driveway`) |
| Gemini API Key(s) | One or more keys, comma-separated for rotation |
| Telegram Bot Token | From [@BotFather](https://t.me/BotFather) |
| Telegram Chat ID | The chat or group to send alerts to |
| Message Thread ID | Optional — for Telegram topic groups |
| AI Prompt | What to ask the AI about each alert image |
| Blue Iris URL | e.g. `http://192.168.1.100:81` (required for video) |
| BI Username / Password | Blue Iris credentials (required for video) |
| Instant notify | Send photo immediately with "Motion detected.", update caption when AI responds (optional) |
| DVLA API Key | Optional — enables automatic number plate enrichment for UK plates |
| Recovery URL | URL of the `bi_recovery.py` endpoint on your Windows host (optional — enables automated encoder restart) |
| Recovery Token | Secret token matching `BI_RECOVERY_SECRET` on the Windows host |

### 3. Configure Blue Iris

The web UI shows the exact **curl parameters** to paste into Blue Iris for each configuration.

1. Open Blue Iris → Camera Settings → **Alerts** tab
2. Under **On alert**, add a **Run a program or write to a file** action
3. Set **File** to `curl.exe`
4. Set **Parameters** to the value shown in the web UI (copy button provided)
5. Set **Window** to `Hide`
6. Uncheck **Wait for process to complete**

Replace `<AlertsFolder>` in the parameters with your Blue Iris alerts path (found in **Global Settings → Storage**).

## Telegram Bot Commands

Once running, send these commands in your alert chat:

| Command | Description |
|---------|-------------|
| `/mute <minutes>` | Mute all cameras |
| `/mute <camera> <minutes>` | Mute one camera |
| `/unmute` | Unmute all |
| `/unmute <camera>` | Unmute one camera |
| `/status` | Show active mutes and caption mode |
| `/caption hilarious\|witty\|rude [minutes]` | Set caption style |
| `/caption off` | Reset to normal captions |
| `/help` | Show command list |

## AI Fallback Chain

Each alert tries AI providers in order until one succeeds:

1. **Gemini** (rotates across keys and models: `gemini-2.5-flash` → `gemini-2.5-flash-lite`)
2. **Grok** (`grok-4-0709`) — optional, add key in configuration
3. **Groq** (`meta-llama/llama-4-scout-17b-16e-instruct`) — optional, add key in configuration

## BI Encoder Recovery

Blue Iris's video export encoder can deadlock after extended uptime (typically 3+ weeks), causing clip exports to stall indefinitely. The hub detects this automatically and can trigger a remote restart of the Blue Iris Windows service.

### Setup

**On your Windows machine**, run `bi_recovery.py` as a startup task:

1. Copy `bi_recovery.py` to your Blue Iris machine
2. Set a strong secret token:
   ```powershell
   $env:BI_RECOVERY_SECRET = "your-secret-here"
   ```
3. Register it as a Task Scheduler startup task (run as Administrator):
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File register_bi_recovery.ps1
   ```
   Or start it manually:
   ```powershell
   $env:BI_RECOVERY_SECRET = "your-secret-here"
   python bi_recovery.py
   ```
   The endpoint listens on port `9090` by default (override with `BI_RECOVERY_PORT`).

**In the hub UI**, edit each camera configuration and set:
- **Recovery URL** — `http://<windows-ip>:9090/restart-bi`
- **Recovery Token** — the same secret you set in `BI_RECOVERY_SECRET`

### How it works

If a clip export has not progressed through the BI export queue within 180 seconds, the hub concludes the encoder is stuck and:

1. POSTs to the recovery endpoint on your Windows machine
2. The Windows service is force-stopped and restarted (~13 seconds)
3. The hub re-submits the export and retries the download

Configs without a Recovery URL set skip this step and the export fails gracefully.

> **Tip:** Also set up a weekly scheduled restart as a preventive measure — this stops the encoder reaching the deadlock state in the first place.

## Updating

When a new version is released, the web UI shows an update banner. Run:

```bash
docker compose pull
docker compose up -d
```

Do not run `docker compose down -v` unless you intentionally want to delete persisted Redis state, including staged BI export jobs.

## Android TV Overlay

The hub can push camera popups to an Android TV running the bundled `Blue Iris AI Hub TV` receiver app. A camera can target one or more paired TVs, and the same webhook flow that drives Telegram alerts can also trigger TV overlays.

### Setup

1. Open the hub dashboard and copy the `TV App Downloader URL`.
2. On the TV, open the `Downloader` app and install `Blue Iris AI Hub TV`.
3. Launch `Blue Iris AI Hub TV` on the TV and note the pairing code it shows.
4. In the hub dashboard, pair the TV using its IP address and pairing code.
5. Edit the camera config and enable `Push stream to TV overlay`.

### Stream Types

- `RTSP (manual URL)`: enter the camera RTSP details in the camera config.
- `Blue Iris MJPG (via proxy)`: the hub builds a proxy stream URL for the TV from `BASE_URL`.

If you use `Blue Iris MJPG (via proxy)`, `BASE_URL` is required. Set it in a `.env` file to the exact hub address the TV can reach:

```env
BASE_URL=http://192.168.0.51:5000
```

The provided `docker-compose.yml` reads that one value and passes it into both `web` and `worker`.

Compose will fail fast if `BASE_URL` is missing.

Relevant Compose snippet:

```yaml
environment:
  - REDIS_URL=redis://redis:6379/0
  - BASE_URL=${BASE_URL:?Set BASE_URL in .env to the hub URL reachable by TVs}
```

For MJPG cameras, the TV overlay uses the hub proxy endpoint:

```text
http://<hub-host>:5000/bi-mjpg/<config_id>
```

Example:

```text
http://192.168.0.51:5000/bi-mjpg/6822c0f9-5deb-431f-b853-50e40a155327
```

## Redis Persistence

The staged BI export pipeline stores active export/download/delivery coordination in Redis. This is not just a transient cache anymore.

The default `docker-compose.yml` now enables Redis append-only persistence and mounts a named Docker volume:

- `redis_data:/data`
- `redis-server --appendonly yes --appendfsync everysec`

That means normal container restarts and `docker compose down` / `up -d` cycles keep BI pipeline state intact. If you remove Docker volumes, you also remove Redis state and may orphan in-flight BI jobs.

## Health and Status

The hub now exposes:

- `GET /health` — lightweight health probe for Docker healthchecks; returns `503` if Redis is unavailable
- `GET /status` — operator-facing JSON with queue depths, stale BI job counts, and background service heartbeat freshness

The Compose file also adds healthchecks for:

- `redis`
- `web`
- `worker`
- `mute_bot`
- `bi_exporter`
- `bi_queue_monitor`
- `bi_downloader`
- `bi_watchdog`
- `video_delivery_worker`

## Architecture

| Service | Description |
|---------|-------------|
| `web` | Flask app — webhook receiver and configuration UI (gunicorn) |
| `worker` | Background task processor (Redis Queue) — handles AI analysis and Telegram sends |
| `bi_exporter` | Blue Iris export submitter — starts BI exports and records staged export jobs |
| `bi_queue_monitor` | Shared BI queue poller — watches `_export` and promotes finished jobs to download |
| `bi_downloader` | Blue Iris downloader — validates and downloads completed exports |
| `bi_watchdog` | Stranded-job watchdog — repairs stale export, download, and delivery states |
| `video_delivery_worker` | Telegram delivery worker — replaces the still image with video and updates the caption after BI download completes |
| `mute_bot` | Telegram bot polling loop — handles `/mute`, `/unmute`, `/caption` commands |
| `redis` | Job queue and state store (mutes, caption modes, API key rotation, export jobs) |

```mermaid
graph LR
    BI[Blue Iris\nWindows] -->|motion alert| web
    web -->|queue job| worker
    worker -->|still photo + AI caption| TG[Telegram]
    worker -->|triggers video pipeline| VP[Video Pipeline\nbi_exporter → bi_queue_monitor\n→ bi_downloader → video_delivery_worker]
    VP -->|fetch clip| BI
    VP -->|video + updated caption| TG
    worker & VP -->|AI analysis| AI[Gemini / Grok / Groq]
    worker & VP -->|plate lookup| DVLA[DVLA API]
    mute_bot -->|/mute /unmute /caption| TG
    bi_watchdog -. monitors .-> VP
```
