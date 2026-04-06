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

### 1. Clone and start

```bash
git clone https://github.com/slflowfoon/blueiris-ai-hub.git
cd blueiris-ai-hub
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

Alert flow: Blue Iris → `curl` POST to `/webhook/<id>` → image saved → "Analysing..." sent to Telegram → job queued → worker analyses with AI → caption updated → video fetched and sent (if enabled).
