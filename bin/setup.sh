#!/usr/bin/env bash
# Setup local development environment for browser-use.

set -o errexit
set -o errtrace
set -o nounset
set -o pipefail
IFS=$'\n'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

VERBOSE=0
LOG_FILE="$SCRIPT_DIR/../.bin-setup.log"

usage() {
    cat <<'EOF'
Usage:
  ./bin/setup.sh [--verbose]

Options:
  --verbose    Print full command output instead of compact logs
  --help       Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --verbose)
            VERBOSE=1
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "[bin/setup][error] Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
    shift
done

: >"$LOG_FILE"

log_info() {
    echo "[bin/setup] $*"
}

log_ok() {
    echo "[bin/setup][ok] $*"
}

run_step() {
    local description="$1"
    shift
    log_info "$description"

    if [[ "$VERBOSE" == "1" ]]; then
        if "$@"; then
            return
        fi
    else
        if "$@" >>"$LOG_FILE" 2>&1; then
            return
        fi
    fi

    echo "[bin/setup][error] $description failed" >&2
    if [[ "$VERBOSE" != "1" ]]; then
        echo "[bin/setup] Last 40 log lines from $LOG_FILE:" >&2
        tail -n 40 "$LOG_FILE" >&2 || true
    fi
    exit 1
}

if [ -f "$SCRIPT_DIR/lint.sh" ]; then
    log_ok "already inside a cloned browser-use repo"
else
    run_step "cloning browser-use repository" git clone https://github.com/browser-use/browser-use
    cd browser-use || exit 1
fi

if [[ "$VERBOSE" == "1" ]]; then
    log_info "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
else
    run_step "installing uv" bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

run_step "creating virtual environment" uv venv
run_step "syncing development dependencies" uv sync --dev --all-extras
run_step "validating browser-use install" uv pip show browser-use

echo
log_ok "setup completed"
echo "Tip: set BROWSER_USE_LOGGING_LEVEL and API keys in .env as needed."
echo ""
echo "Usage:"
echo "  browser-use"
echo "  source .venv/bin/activate"
echo "  ipython"
echo "  >>> from browser_use import BrowserSession, Agent"
echo "  >>> await Agent(task='book me a flight to fiji', browser=BrowserSession(headless=False)).run()"
echo
