#!/usr/bin/env bash
# install_deps.sh — Install optional dependencies for Gateway built-in tools.
# Usage: ./scripts/install_deps.sh [--check]
#   --check  Only check what's installed, don't install anything.
#
# These are optional — the gateway core works with Python stdlib only.
# Install/check only dependencies used by current built-in tools; heavier
# integrations should be added as MCP servers or HTTP Actions.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true
MISSING_COUNT=0

ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
miss() { echo -e "  ${RED}[MISSING]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; }

install() {
    local name="$1"
    local brew_pkg="${2:-$1}"
    if command -v "$name" &>/dev/null; then
        ok "$name"
        return 0
    fi
    miss "$name"
    if $CHECK_ONLY; then
        MISSING_COUNT=$((MISSING_COUNT + 1))
        return 0
    fi
    if command -v brew &>/dev/null; then
        echo "  -> brew install $brew_pkg"
        brew install "$brew_pkg"
    elif command -v apt-get &>/dev/null; then
        echo "  -> sudo apt-get install -y $brew_pkg"
        sudo apt-get install -y "$brew_pkg"
    else
        warn "No brew or apt-get found, install $name manually"
        return 1
    fi
}

pip_install() {
    local name="$1"
    local import_name="${2:-$1}"
    if python3 -c "import $import_name" 2>/dev/null; then
        ok "$name (pip)"
        return 0
    fi
    miss "$name (pip)"
    if $CHECK_ONLY; then
        MISSING_COUNT=$((MISSING_COUNT + 1))
        return 0
    fi
    echo "  -> pip3 install $name"
    pip3 install "$name"
}

echo "=== Gateway Built-in Tools: Dependency Check ==="
echo ""

echo "[CLI Binaries]"
install git
install jq jq

echo ""
echo "[Python Packages]"
pip_install Pillow PIL
pip_install pyautogui pyautogui
if [[ "$(uname -s)" == "Darwin" ]]; then
    pip_install pyobjc-framework-Quartz Quartz
fi

echo ""
echo "[Summary]"
TOOLS_STATUS=""

check_tool() {
    local name="$1"
    local cmd="${2:-$1}"
    if command -v "$cmd" &>/dev/null; then
        TOOLS_STATUS="$TOOLS_STATUS  [OK] $name\n"
    else
        TOOLS_STATUS="$TOOLS_STATUS  [--] $name\n"
    fi
}

check_tool "Read/Write/Edit/Glob/Grep/LS/Tree" "python3"
check_tool "Bash/shell exec"            "python3"
check_tool "Git operations"             "git"
check_tool "Image metadata/screenshot"  "python3"
check_tool "GUI automation backend"     "python3"
check_tool "Web search (DuckDuckGo)"    "python3"
check_tool "Web fetch/HTTP"             "python3"
check_tool "Calculator/time"            "python3"
check_tool "MCP servers"                "python3"

echo ""
echo -e "$TOOLS_STATUS"

echo "Done. Gateway core needs only Python 3.9+."
echo "Optional tools above add image metadata and desktop automation support."
if $CHECK_ONLY && [[ "$MISSING_COUNT" -gt 0 ]]; then
    echo "Missing optional dependencies: $MISSING_COUNT"
    exit 1
fi
