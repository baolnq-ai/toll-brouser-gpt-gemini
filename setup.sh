#!/usr/bin/env bash

set -o errexit
set -o errtrace
set -o nounset
set -o pipefail
IFS=$'\n'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$ROOT_DIR"

SETUP_ONLY=0

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
	cat <<'EOF'
Usage:
	./setup.sh            Setup dependencies and start the Gemini/GPT FastAPI bridge
  ./setup.sh --setup-only

Environment variables:
	GEMINI_CDP_URL            Default: http://127.0.0.1:9222
	GEMINI_API_HOST           Default: 0.0.0.0
	GEMINI_API_PORT           Default: 8008
	GEMINI_API_LOG_LEVEL      Default: info
	Ports auto-jump to a free value when possible to avoid conflicts
  AUTO_LAUNCH_CHROME        Default: 0 (set to 1 to auto launch Chrome with debug port)
  CHROME_BIN                Optional explicit Chrome binary path
EOF
	exit 0
fi

if [[ "${1:-}" == "--setup-only" ]]; then
	SETUP_ONLY=1
fi

log() {
	echo "[setup] $*"
}

port_is_free() {
	local python_bin="$1"
	local host="$2"
	local port="$3"

	"$python_bin" - "$host" "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
	sock.bind((host, port))
except OSError:
	raise SystemExit(1)
finally:
	sock.close()
PY
}

find_free_port() {
	local python_bin="$1"
	local host="$2"
	local start_port="$3"

	"$python_bin" - "$host" "$start_port" <<'PY'
import socket
import sys

host = sys.argv[1]
start_port = int(sys.argv[2])

for port in range(start_port, start_port + 200):
	sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	try:
		sock.bind((host, port))
	except OSError:
		sock.close()
		continue
	else:
		sock.close()
		print(port)
		raise SystemExit(0)

raise SystemExit(1)
PY
}

replace_url_port() {
	local python_bin="$1"
	local url="$2"
	local new_port="$3"

	"$python_bin" - "$url" "$new_port" <<'PY'
from urllib.parse import urlparse, urlunparse
import sys

url = sys.argv[1]
new_port = int(sys.argv[2])
parsed = urlparse(url)
host = parsed.hostname or '127.0.0.1'
scheme = parsed.scheme or 'http'

if ':' in host and not host.startswith('['):
	host = f'[{host}]'

netloc = f'{host}:{new_port}'
print(urlunparse((scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)))
PY
}

ensure_uv() {
	if command -v uv >/dev/null 2>&1; then
		return
	fi

	log "uv not found, installing uv"
	curl -LsSf https://astral.sh/uv/install.sh | sh

	if [[ -x "$HOME/.local/bin/uv" ]]; then
		export PATH="$HOME/.local/bin:$PATH"
	fi

	if ! command -v uv >/dev/null 2>&1; then
		echo "[setup] ERROR: uv is not available after installation" >&2
		exit 1
	fi
}

resolve_venv_python() {
	if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
		echo "$ROOT_DIR/.venv/bin/python"
		return
	fi

	if [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
		echo "$ROOT_DIR/.venv/Scripts/python.exe"
		return
	fi

	echo ""
}

ensure_venv_and_deps() {
	if [[ ! -d "$ROOT_DIR/.venv" ]]; then
		log "creating virtual environment"
		uv venv --python 3.11
	fi

	log "syncing project dependencies"
	uv sync --dev --all-extras

	VENV_PYTHON="$(resolve_venv_python)"
	if [[ -z "$VENV_PYTHON" ]]; then
		echo "[setup] ERROR: cannot locate virtual environment python" >&2
		exit 1
	fi

	if ! "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1
import fastapi
import uvicorn
PY
	then
		log "installing fastapi and uvicorn into project venv"
		uv pip install --python "$VENV_PYTHON" fastapi uvicorn
	fi
}

extract_cdp_port() {
	local cdp_url="$1"
	local port
	port="$(printf '%s' "$cdp_url" | sed -E 's#.*:([0-9]+).*#\1#')"
	if [[ -z "$port" ]]; then
		port="9222"
	fi
	echo "$port"
}

check_cdp_endpoint() {
	local cdp_url="$1"
	curl -fsS "$cdp_url/json/version" >/dev/null 2>&1
}

find_chrome_bin() {
	if [[ -n "${CHROME_BIN:-}" ]]; then
		echo "$CHROME_BIN"
		return
	fi

	local candidates=(
		"/c/Program Files/Google/Chrome/Application/chrome.exe"
		"/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"
		"/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
		"/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"
	)

	for path in "${candidates[@]}"; do
		if [[ -x "$path" ]]; then
			echo "$path"
			return
		fi
	done

	for cmd in google-chrome chromium-browser chromium; do
		if command -v "$cmd" >/dev/null 2>&1; then
			echo "$cmd"
			return
		fi
	done

	echo ""
}

maybe_launch_chrome_debug() {
	local python_bin="$1"
	local cdp_url="$2"
	local requested_cdp_port
	local cdp_port
	requested_cdp_port="$(extract_cdp_port "$cdp_url")"
	cdp_port="$requested_cdp_port"

	if check_cdp_endpoint "$cdp_url"; then
		log "CDP endpoint is ready at $cdp_url"
		return
	fi

	if [[ "${AUTO_LAUNCH_CHROME:-0}" == "1" ]] && ! port_is_free "$python_bin" "127.0.0.1" "$requested_cdp_port"; then
		cdp_port="$(find_free_port "$python_bin" "127.0.0.1" "$requested_cdp_port")"
		cdp_url="$(replace_url_port "$python_bin" "$cdp_url" "$cdp_port")"
		export GEMINI_CDP_URL="$cdp_url"
		log "CDP port ${requested_cdp_port} is busy, switched Chrome debug port to ${cdp_port}"
	fi

	if [[ "${AUTO_LAUNCH_CHROME:-0}" != "1" ]]; then
		cat <<EOF
[setup] Chrome CDP endpoint is not reachable at $cdp_url
[setup] Start Chrome with debug port, for example:
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=$cdp_port --user-data-dir="C:\\tmp\\chrome-gemini-debug"
[setup] Then open Gemini tab and login.
EOF
		exit 1
	fi

	local chrome_bin
	chrome_bin="$(find_chrome_bin)"
	if [[ -z "$chrome_bin" ]]; then
		echo "[setup] ERROR: AUTO_LAUNCH_CHROME=1 but no Chrome binary found" >&2
		exit 1
	fi

	local profile_dir="$ROOT_DIR/.chrome-debug-profile"
	mkdir -p "$profile_dir"

	log "launching Chrome with remote debugging on port $cdp_port"
	"$chrome_bin" --remote-debugging-port="$cdp_port" --user-data-dir="$profile_dir" >/dev/null 2>&1 &

	for _ in $(seq 1 20); do
		if check_cdp_endpoint "$cdp_url"; then
			log "CDP endpoint is ready at $cdp_url"
			return
		fi
		sleep 1
	done

	echo "[setup] ERROR: Chrome launched but CDP endpoint is still not reachable at $cdp_url" >&2
	exit 1
}

start_server() {
	local venv_python="$1"
	local requested_api_port
	local free_api_port

	export GEMINI_CDP_URL="${GEMINI_CDP_URL:-http://127.0.0.1:9222}"
	export GEMINI_API_HOST="${GEMINI_API_HOST:-0.0.0.0}"
	export GEMINI_API_PORT="${GEMINI_API_PORT:-8008}"
	export GEMINI_API_LOG_LEVEL="${GEMINI_API_LOG_LEVEL:-info}"

	requested_api_port="$GEMINI_API_PORT"
	if ! port_is_free "$venv_python" "$GEMINI_API_HOST" "$requested_api_port"; then
		free_api_port="$(find_free_port "$venv_python" "$GEMINI_API_HOST" "$requested_api_port")"
		log "API port ${requested_api_port} is busy, switched to ${free_api_port}"
		export GEMINI_API_PORT="$free_api_port"
	fi

	maybe_launch_chrome_debug "$venv_python" "$GEMINI_CDP_URL"

	log "starting FastAPI server on ${GEMINI_API_HOST}:${GEMINI_API_PORT}"
	"$venv_python" "$ROOT_DIR/examples/apps/gemini-use/server.py"
}

ensure_uv
ensure_venv_and_deps

VENV_PYTHON="$(resolve_venv_python)"
if [[ -z "$VENV_PYTHON" ]]; then
	echo "[setup] ERROR: cannot resolve venv python after setup" >&2
	exit 1
fi

if [[ "$SETUP_ONLY" == "1" ]]; then
	log "setup completed"
	exit 0
fi

start_server "$VENV_PYTHON"
