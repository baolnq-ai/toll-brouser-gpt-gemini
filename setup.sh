#!/usr/bin/env bash

set -o errexit
set -o errtrace
set -o nounset
set -o pipefail
IFS=$'\n'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$ROOT_DIR"

SETUP_ONLY=0
ASSUME_YES=0
OS_FAMILY="unknown"
SETUP_VERBOSE="${SETUP_VERBOSE:-0}"
SETUP_LOG_FILE="$ROOT_DIR/.setup.log"
declare -a PYTHON_RUNTIME_CMD=()
PYTHON_RUNTIME_LABEL=""

: >"$SETUP_LOG_FILE"

usage() {
	cat <<'EOF'
Usage:
	./setup.sh            Setup dependencies and start the Gemini/GPT FastAPI bridge
	./setup.sh --setup-only
	./setup.sh --yes

Environment variables:
	Config is loaded in this order: .env.example, then .env (if present)
	GEMINI_CDP_URL            Default: http://127.0.0.1:9222
	CHAT_BRIDGE_DISCOVERY_PORTS Default: 9222,9223,9224
	GEMINI_OPEN_URL           Default: https://gemini.google.com/app
	GPT_OPEN_URL              Default: https://chatgpt.com/
	GEMINI_DEFAULT_TIMEOUT_S  Default: 600
	GPT_DEFAULT_TIMEOUT_S     Default: 600
	GEMINI_API_HOST           Default: 0.0.0.0
	GEMINI_API_PORT           Default: 8008
	GEMINI_API_LOG_LEVEL      Default: info
	AUTO_LAUNCH_CHROME        Default: 0 (set to 1 to auto launch Chrome with debug port)
	STRICT_CDP_STARTUP        Default: 0 (set to 1 to fail setup when CDP is unavailable)
	CHROME_BIN                Optional explicit Chrome binary path
EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--help|-h)
			usage
			exit 0
			;;
		--setup-only)
			SETUP_ONLY=1
			;;
		--yes|-y)
			ASSUME_YES=1
			;;
		*)
			echo "[setup] ERROR: unknown option: $1" >&2
			usage
			exit 1
			;;
	esac
	shift
done

log() {
	echo "[setup] $*"
}

log_ok() {
	echo "[setup][ok] $*"
}

log_warn() {
	echo "[setup][warn] $*"
}

log_error() {
	echo "[setup][error] $*" >&2
}

run_step() {
	local description="$1"
	shift

	log "$description"
	if [[ "$SETUP_VERBOSE" == "1" ]]; then
		if "$@"; then
			return
		fi
	else
		if "$@" >>"$SETUP_LOG_FILE" 2>&1; then
			return
		fi
	fi

	log_error "$description failed"
	if [[ "$SETUP_VERBOSE" != "1" ]]; then
		echo "[setup] Last 60 log lines from $SETUP_LOG_FILE:" >&2
		tail -n 60 "$SETUP_LOG_FILE" >&2 || true
	fi
	exit 1
}

load_env_file() {
	local env_file="$1"
	local overwrite="$2"

	if [[ ! -f "$env_file" ]]; then
		return
	fi

	while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
		local line key value
		line="${raw_line%$'\r'}"

		[[ -z "${line//[[:space:]]/}" ]] && continue
		[[ "$line" =~ ^[[:space:]]*# ]] && continue

		if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
			key="${BASH_REMATCH[1]}"
			value="${BASH_REMATCH[2]}"

			if [[ "$value" =~ ^\"(.*)\"$ ]]; then
				value="${BASH_REMATCH[1]}"
			elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
				value="${BASH_REMATCH[1]}"
			fi

			if [[ "$overwrite" == "1" || -z "${!key+x}" ]]; then
				export "$key=$value"
			fi
		fi
	done <"$env_file"
}

load_runtime_env() {
	local example_env="$ROOT_DIR/.env.example"
	local project_env="$ROOT_DIR/.env"

	load_env_file "$example_env" "0"
	if [[ -f "$project_env" ]]; then
		load_env_file "$project_env" "1"
		log "loaded runtime config from .env (defaults from .env.example)"
	else
		log "loaded runtime defaults from .env.example"
	fi
}

detect_os() {
	case "$(uname -s)" in
		Linux*)
			OS_FAMILY="linux"
			;;
		Darwin*)
			OS_FAMILY="macos"
			;;
		MINGW*|MSYS*|CYGWIN*)
			OS_FAMILY="windows"
			;;
		*)
			OS_FAMILY="unknown"
			;;
	esac
}

ask_yes_no() {
	local prompt="$1"
	if [[ "$ASSUME_YES" == "1" ]]; then
		log "$prompt -> yes (auto)"
		return 0
	fi

	local answer
	read -r -p "$prompt [y/N] " answer
	case "${answer,,}" in
		y|yes)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

run_with_sudo() {
	if [[ "$(id -u)" == "0" ]]; then
		"$@"
		return
	fi

	if command -v sudo >/dev/null 2>&1; then
		sudo "$@"
		return
	fi

	echo "[setup] ERROR: need root privileges to run: $*" >&2
	exit 1
}

is_python_compatible() {
	"$@" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

set_python_runtime_cmd() {
	local label arg
	PYTHON_RUNTIME_CMD=("$@")
	label="$1"
	shift
	for arg in "$@"; do
		label="$label $arg"
	done
	PYTHON_RUNTIME_LABEL="$label"
}

find_python_cmd() {
	local candidate
	for candidate in python3.12 python3.11 python3 python; do
		if command -v "$candidate" >/dev/null 2>&1 && is_python_compatible "$candidate"; then
			set_python_runtime_cmd "$candidate"
			return 0
		fi
	done

	if command -v py >/dev/null 2>&1; then
		for candidate in -3.12 -3.11 -3; do
			if is_python_compatible py "$candidate"; then
				set_python_runtime_cmd py "$candidate"
				return 0
			fi
		done
	fi

	return 1
}

ensure_brew() {
	if command -v brew >/dev/null 2>&1; then
		return
	fi

	if ! ask_yes_no "Homebrew is required but not installed. Install Homebrew now?"; then
		echo "[setup] ERROR: Homebrew is required to continue on macOS" >&2
		exit 1
	fi

	if [[ "$SETUP_VERBOSE" == "1" ]]; then
		/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
	else
		/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" >>"$SETUP_LOG_FILE" 2>&1
	fi
	if [[ -x /opt/homebrew/bin/brew ]]; then
		eval "$(/opt/homebrew/bin/brew shellenv)"
	elif [[ -x /usr/local/bin/brew ]]; then
		eval "$(/usr/local/bin/brew shellenv)"
	fi

	if ! command -v brew >/dev/null 2>&1; then
		echo "[setup] ERROR: Homebrew installation did not complete" >&2
		exit 1
	fi
}

install_python_for_os() {
	case "$OS_FAMILY" in
		windows)
			if command -v winget >/dev/null 2>&1; then
				powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements"
			elif command -v choco >/dev/null 2>&1; then
				choco install -y python --version=3.11.9
			else
				echo "[setup] ERROR: no supported package manager found on Windows (winget/choco)" >&2
				exit 1
			fi
			;;
		macos)
			ensure_brew
			brew install python@3.11
			;;
		linux)
			if command -v apt-get >/dev/null 2>&1; then
				run_with_sudo apt-get update
				run_with_sudo apt-get install -y python3 python3-venv python3-pip
			elif command -v dnf >/dev/null 2>&1; then
				run_with_sudo dnf install -y python3 python3-pip
			elif command -v yum >/dev/null 2>&1; then
				run_with_sudo yum install -y python3 python3-pip
			elif command -v pacman >/dev/null 2>&1; then
				run_with_sudo pacman -Sy --noconfirm python python-pip
			else
				echo "[setup] ERROR: no supported Linux package manager found (apt/dnf/yum/pacman)" >&2
				exit 1
			fi
			;;
		*)
			echo "[setup] ERROR: unsupported OS for automatic Python installation" >&2
			exit 1
			;;
	esac
}

ensure_python_runtime() {
	if find_python_cmd; then
		local version
		version="$("${PYTHON_RUNTIME_CMD[@]}" --version 2>&1 || true)"
		log_ok "python runtime detected: ${PYTHON_RUNTIME_LABEL} (${version:-unknown version})"
		return
	fi

	if ! ask_yes_no "Python >=3.11 not found. Install now?"; then
		echo "[setup] ERROR: Python >=3.11 is required" >&2
		exit 1
	fi

	log "installing Python runtime"
	install_python_for_os
	hash -r

	if ! find_python_cmd; then
		echo "[setup] ERROR: Python installation completed but Python >=3.11 still not found" >&2
		exit 1
	fi
}

install_node_for_os() {
	case "$OS_FAMILY" in
		windows)
			if command -v winget >/dev/null 2>&1; then
				powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements"
			elif command -v choco >/dev/null 2>&1; then
				choco install -y nodejs-lts
			else
				echo "[setup] ERROR: no supported package manager found on Windows (winget/choco)" >&2
				exit 1
			fi
			;;
		macos)
			ensure_brew
			brew install node
			;;
		linux)
			if command -v apt-get >/dev/null 2>&1; then
				run_with_sudo apt-get update
				run_with_sudo apt-get install -y nodejs npm
			elif command -v dnf >/dev/null 2>&1; then
				run_with_sudo dnf install -y nodejs npm
			elif command -v yum >/dev/null 2>&1; then
				run_with_sudo yum install -y nodejs npm
			elif command -v pacman >/dev/null 2>&1; then
				run_with_sudo pacman -Sy --noconfirm nodejs npm
			else
				echo "[setup] ERROR: no supported Linux package manager found (apt/dnf/yum/pacman)" >&2
				exit 1
			fi
			;;
		*)
			echo "[setup] ERROR: unsupported OS for automatic Node.js installation" >&2
			exit 1
			;;
	esac
}

ensure_node_runtime() {
	if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
		log_ok "node runtime detected: $(node --version), npm $(npm --version)"
		return
	fi

	if ! ask_yes_no "Node.js (node + npm) not found. Install now?"; then
		echo "[setup] ERROR: Node.js is required" >&2
		exit 1
	fi

	log "installing Node.js runtime"
	install_node_for_os
	hash -r

	if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
		echo "[setup] ERROR: Node.js installation completed but node/npm is still not available" >&2
		exit 1
	fi
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
if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
	sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
try:
	sock.bind((host, port))
except OSError:
	raise SystemExit(1)
finally:
	sock.close()
PY
}

ensure_uv() {
	if command -v uv >/dev/null 2>&1; then
		return
	fi

	log "uv not found, installing uv"
	if [[ "$SETUP_VERBOSE" == "1" ]]; then
		curl -LsSf https://astral.sh/uv/install.sh | sh
	else
		curl -LsSf https://astral.sh/uv/install.sh | sh >>"$SETUP_LOG_FILE" 2>&1
	fi

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
		run_step "creating virtual environment" uv venv --python 3.11
	fi

	run_step "syncing project dependencies" uv sync --dev --all-extras

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
		run_step "installing fastapi and uvicorn into project venv" uv pip install --python "$VENV_PYTHON" fastapi uvicorn
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

extract_url_host() {
	local python_bin="$1"
	local raw_url="$2"

	"$python_bin" - "$raw_url" <<'PY'
from urllib.parse import urlparse
import sys

parsed = urlparse(sys.argv[1])
print(parsed.hostname or '127.0.0.1')
PY
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
	local launch_now
	local requested_cdp_port
	local cdp_port
	local cdp_host
	launch_now="${AUTO_LAUNCH_CHROME:-0}"
	requested_cdp_port="$(extract_cdp_port "$cdp_url")"
	cdp_port="$requested_cdp_port"
	cdp_host="$(extract_url_host "$python_bin" "$cdp_url")"

	if check_cdp_endpoint "$cdp_url"; then
		log_ok "CDP endpoint is ready at $cdp_url"
		return
	fi

	if [[ "$cdp_host" == "localhost" || "$cdp_host" == "127.0.0.1" ]] && ! port_is_free "$python_bin" "$cdp_host" "$requested_cdp_port"; then
		log_error "CDP port ${requested_cdp_port} is already in use and endpoint ${cdp_url}/json/version is not reachable"
		echo "[setup] Resolve by freeing port ${requested_cdp_port} or updating GEMINI_CDP_URL in .env/.env.example" >&2
		exit 1
	fi

	if [[ "$launch_now" != "1" && "$ASSUME_YES" != "1" ]] && [[ -t 0 ]]; then
		if ask_yes_no "Chrome CDP endpoint is not reachable at $cdp_url. Launch Chrome automatically now?"; then
			launch_now="1"
		fi
	fi

	if [[ "$launch_now" != "1" ]]; then
		cat <<EOF
[setup] Chrome CDP endpoint is not reachable at $cdp_url
[setup] Start Chrome with debug port, for example:
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=$cdp_port --user-data-dir="C:\\tmp\\chrome-gemini-debug"
[setup] Then open Gemini tab and login.
EOF
		if [[ "${STRICT_CDP_STARTUP:-0}" == "1" ]]; then
			exit 1
		fi
		log_warn "continuing without active CDP endpoint (chat/image calls will fail until Chrome debug is available)"
		return
	fi

	export AUTO_LAUNCH_CHROME="1"

	local chrome_bin
	chrome_bin="$(find_chrome_bin)"
	if [[ -z "$chrome_bin" ]]; then
		echo "[setup] ERROR: AUTO_LAUNCH_CHROME=1 but no Chrome binary found" >&2
		if [[ "${STRICT_CDP_STARTUP:-0}" == "1" ]]; then
			exit 1
		fi
		log "continuing without active CDP endpoint"
		return
	fi

	local profile_dir="$ROOT_DIR/.chrome-debug-profile"
	mkdir -p "$profile_dir"

	log "launching Chrome with remote debugging on port $cdp_port"
	"$chrome_bin" --remote-debugging-port="$cdp_port" --user-data-dir="$profile_dir" >/dev/null 2>&1 &

	for _ in $(seq 1 20); do
		if check_cdp_endpoint "$cdp_url"; then
			log_ok "CDP endpoint is ready at $cdp_url"
			return
		fi
		sleep 1
	done

	echo "[setup] ERROR: Chrome launched but CDP endpoint is still not reachable at $cdp_url" >&2
	if [[ "${STRICT_CDP_STARTUP:-0}" == "1" ]]; then
		exit 1
	fi
	log_warn "continuing without active CDP endpoint"
}

start_server() {
	local venv_python="$1"
	local requested_api_port

	export GEMINI_CDP_URL="${GEMINI_CDP_URL:-http://127.0.0.1:9222}"
	export GEMINI_API_HOST="${GEMINI_API_HOST:-0.0.0.0}"
	export GEMINI_API_PORT="${GEMINI_API_PORT:-8008}"
	export GEMINI_API_LOG_LEVEL="${GEMINI_API_LOG_LEVEL:-info}"

	requested_api_port="$GEMINI_API_PORT"
	if ! port_is_free "$venv_python" "$GEMINI_API_HOST" "$requested_api_port"; then
		log_error "API port ${requested_api_port} is already in use on ${GEMINI_API_HOST}"
		echo "[setup] Update GEMINI_API_PORT in .env/.env.example or stop the process using that port." >&2
		exit 1
	fi

	maybe_launch_chrome_debug "$venv_python" "$GEMINI_CDP_URL"

	log "starting FastAPI server on ${GEMINI_API_HOST}:${GEMINI_API_PORT}"
	"$venv_python" "$ROOT_DIR/examples/apps/gemini-use/server.py"
}

ensure_uv
load_runtime_env
detect_os
log "detected OS: $OS_FAMILY"
ensure_python_runtime
ensure_node_runtime
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
