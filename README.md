# toll-brouser-gpt-gemini

Local-first Browser Use fork focused on running Gemini and GPT web automation through a single FastAPI bridge.

This repository is tailored for:
- Local runtime on Windows/Linux/macOS
- Free-first workflow (use web tabs and your own browser session)
- One-command bring-up with dependency setup and server startup
- Stable CDP handling for multi-port endpoint open

## What Is Included

- Bridge API server: [examples/apps/gemini-use/server.py](examples/apps/gemini-use/server.py)
- Setup and bring-up script: [setup.sh](setup.sh)
- Core Browser Use library source: [browser_use](browser_use)

The bridge exposes endpoints for:
- Opening provider tabs via CDP
- Sending chat prompts to Gemini/GPT tabs
- Triggering image generation flows
- Checking and closing managed CDP ports

## Quick Start

### 1) Clone

```bash
git clone https://github.com/baolnq-ai/toll-brouser-gpt-gemini.git
cd toll-brouser-gpt-gemini
```

### 2) Setup and run

```bash
./setup.sh
```

What setup does:
- Detects runtime requirements (Python, Node, uv)
- Creates/uses local venv
- Installs dependencies with uv
- Starts FastAPI bridge server
- Probes CDP endpoint and can auto-launch Chrome when configured

## Default Runtime Config

- GEMINI_CDP_URL: http://127.0.0.1:9222
- GEMINI_API_HOST: 0.0.0.0
- GEMINI_API_PORT: 8008
- GEMINI_API_LOG_LEVEL: info

Optional:
- AUTO_LAUNCH_CHROME=1 to auto launch Chrome from setup
- CHROME_BIN to point to a specific Chrome executable
- STRICT_CDP_STARTUP=1 to fail fast if CDP is not reachable

Bridge server extras:
- CHAT_BRIDGE_AUTO_LAUNCH_CHROME=1 enables auto-launch fallback for /v1/web/open when requested CDP port is not live
- CHAT_BRIDGE_CHROME_BIN overrides Chrome executable discovery for bridge fallback launch
- CHAT_BRIDGE_CHROME_PROFILE_DIR sets profile root for bridge-launched debug browsers

## API Endpoints

Source: [examples/apps/gemini-use/server.py](examples/apps/gemini-use/server.py)

- POST /v1/web/open
- POST /v1/ports/ping
- GET /v1/ports/ping
- POST /v1/ports/close
- POST /v1/chat/gemini
- POST /v1/chat/gpt
- POST /v1/image/gemini
- POST /v1/image/gpt

OpenAPI docs when server is running:
- http://127.0.0.1:8008/docs

## Example Requests

Open Gemini web tab on a specific CDP port:

```bash
curl -X POST "http://127.0.0.1:8008/v1/web/open" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "gemini",
    "port": 9223,
    "url": "https://gemini.google.com",
    "new_tab": true,
    "force_reconnect": false
  }'
```

Send chat prompt to Gemini:

```bash
curl -X POST "http://127.0.0.1:8008/v1/chat/gemini" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Hello from local bridge",
    "port": 9223
  }'
```

## Troubleshooting

### CDP_CONNECT_FAILED on /v1/web/open

If you request a new port (example: 9223) and CDP is not reachable:
- The bridge now attempts local Chrome auto-launch for that port when CHAT_BRIDGE_AUTO_LAUNCH_CHROME is enabled (default enabled).
- If auto-launch still fails, inspect response details.auto_launch for exact reason.

Useful checks:

```bash
curl "http://127.0.0.1:8008/v1/ports/ping"
```

### Port conflict

setup.sh automatically shifts API/CDP ports when possible.

### Chrome path issues on Windows

Set explicit binary path:

```bash
export CHROME_BIN="/c/Program Files/Google/Chrome/Application/chrome.exe"
```

or for bridge fallback launcher:

```bash
export CHAT_BRIDGE_CHROME_BIN="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
```

## Development

Create environment and sync:

```bash
uv venv --python 3.11
uv sync --dev --all-extras
```

Run bridge directly:

```bash
python examples/apps/gemini-use/server.py
```

## License

MIT, same as this repository's [LICENSE](LICENSE).
