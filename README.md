# Blue Iris AI Hub

AI-powered motion alert processor for [Blue Iris](https://blueirissoftware.com/). When a camera triggers, it analyses the image with Gemini AI, sends a Telegram notification with a caption, and optionally fetches and sends the full video clip.

## Features

- **AI vision analysis** — Gemini 2.5/2.0 Flash with automatic API key rotation and fallback to Grok / Groq
- **Video clips** — exports the alert clip from Blue Iris, analyses it with Gemini, and replaces the still photo in Telegram with the video
- **Telegram notifications** — instant alerts with AI-generated captions; captions are updated in-place when video analysis completes
- **Auto-mute** — silences a camera automatically after 5 triggers in 10 minutes (prevents spam)
- **Caption modes** — switch to `hilarious`, `witty`, or `rude` captions via Telegram bot commands
- **Known plates** — teach the AI to recognise and label your vehicles by number plate
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

1. **Gemini** (rotates across keys and models: `gemini-2.5-flash` → `gemini-2.0-flash` → `gemini-2.0-flash-lite`)
2. **Grok** (`grok-2-vision-1212`) — optional, add key in configuration
3. **Groq** (`llama-3.2-11b-vision-preview`) — optional, add key in configuration

## BI Encoder Recovery

Blue Iris's video export encoder can deadlock after extended uptime (typically 3+ weeks), causing all clip exports to return persistent `503` errors. The hub detects this automatically (30 consecutive empty 503 responses) and can trigger a remote restart of the Blue Iris Windows service.

### Setup

**On your Windows machine**, run `bi_recovery.py` as a startup task:

1. Download [`bi_recovery.py`](app/bi_recovery.py) to your Blue Iris machine
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

If the hub sees 30 consecutive `503` responses with empty `Content-Length` during a video download (~60 seconds), it:

1. POSTs to the recovery endpoint on your Windows machine
2. The Windows service is force-stopped and restarted (~13 seconds)
3. The hub re-logs into Blue Iris, re-exports the clip, and retries the download

Configs without a Recovery URL set skip this step and fall back to the existing retry behaviour.

> **Tip:** Also set up a weekly scheduled restart as a preventive measure — this stops the encoder reaching the deadlock state in the first place.

## Updating

When a new version is released, the web UI shows an update banner. Run:

```bash
docker compose pull
docker compose up -d
```

## Architecture

| Service | Description |
|---------|-------------|
| `web` | Flask app — webhook receiver and configuration UI (gunicorn) |
| `worker` | Background task processor (Redis Queue) — handles AI analysis and Telegram sends |
| `mute_bot` | Telegram bot polling loop — handles `/mute`, `/unmute`, `/caption` commands |
| `redis` | Job queue and state store (mutes, caption modes, API key rotation) |

Alert flow: Blue Iris → `curl` POST to `/webhook/<id>` → image saved → job queued → worker analyses still image with AI → Telegram notification sent with caption → (if video enabled) clip exported from BI, analysed with AI, caption updated and photo replaced with video clip.
