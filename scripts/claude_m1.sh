#!/usr/bin/env bash
set -euo pipefail

# One-command Claude Code launcher through the local Gateway.
# It starts/reuses ./scripts/mimo_gateway.sh, waits for health, then execs Claude
# with the same environment shape as the user's claude_m1 shell function.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${GATEWAY_PORT:-8885}"
CONFIG_PATH="${GATEWAY_CONFIG_PATH:-$ROOT_DIR/.gateway_service.json}"

config_value() {
  local key="$1" fallback="$2"
  ROOT_DIR="$ROOT_DIR" GATEWAY_CONFIG_PATH="$CONFIG_PATH" CONFIG_KEY="$key" CONFIG_FALLBACK="$fallback" python3 - <<'PY'
import os, sys
key = os.environ.get("CONFIG_KEY") or ""
fallback = os.environ.get("CONFIG_FALLBACK") or ""
try:
    sys.path.insert(0, os.environ["ROOT_DIR"])
    from src.gateway_config import load_config
    cfg = load_config()
    cur = cfg
    for part in key.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    print(cur or fallback)
except Exception:
    print(fallback)
PY
}

BASE_URL="${ANTHROPIC_BASE_URL:-http://127.0.0.1:${PORT}/anthropic}"
CONFIG_API_KEY="$(config_value gateway.client_snippet_api_key local-gateway-key)"
CONFIG_MODEL="$(config_value upstream.model mimo-v2.5-pro)"
API_KEY="${ANTHROPIC_AUTH_TOKEN:-${DOWNSTREAM_API_KEY:-$CONFIG_API_KEY}}"
MODEL="${ANTHROPIC_MODEL:-${UPSTREAM_MODEL:-$CONFIG_MODEL}}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"

wait_health() {
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

if ! wait_health; then
  GATEWAY_PORT="$PORT" DOWNSTREAM_API_KEY="$API_KEY" UPSTREAM_MODEL="$MODEL" "$ROOT_DIR/scripts/mimo_gateway.sh" start >/dev/null
fi

if ! wait_health; then
  echo "Gateway did not become healthy at http://127.0.0.1:${PORT}; run ./scripts/mimo_gateway.sh logs" >&2
  exit 1
fi

if [[ ! -x "$CLAUDE_BIN" ]]; then
  echo "Claude binary not found/executable: $CLAUDE_BIN" >&2
  exit 127
fi

export ANTHROPIC_BASE_URL="$BASE_URL"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"
export ANTHROPIC_AUTH_TOKEN="$API_KEY"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-$MODEL}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-$MODEL}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-$MODEL}"
export ANTHROPIC_MODEL="$MODEL"
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$MODEL}"
export ENABLE_LSP_TOOL="${ENABLE_LSP_TOOL:-1}"

exec "$CLAUDE_BIN" --dangerously-skip-permissions "$@"
