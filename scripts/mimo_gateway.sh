#!/usr/bin/env bash
set -euo pipefail

# Local Gateway service for Claude Code / Codex / OpenCode.
# Upstream credentials are read from env or the ignored local .gateway_service.json.
# Claude Code should point ANTHROPIC_BASE_URL to http://127.0.0.1:8885/anthropic.
# Codex should point its OpenAI-compatible provider base_url to http://127.0.0.1:8885/v1.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-start}"
HOST="${GATEWAY_HOST:-127.0.0.1}"
PORT="${GATEWAY_PORT:-8885}"
CONFIG_PATH="${GATEWAY_CONFIG_PATH:-$ROOT_DIR/.gateway_service.json}"
LOG_DIR="${GATEWAY_RUNTIME_DIR:-$ROOT_DIR/.gateway_runtime}"
PID_FILE="$LOG_DIR/gateway-${PORT}.pid"
LOG_FILE="$LOG_DIR/gateway-${PORT}.log"
LAUNCHD_LABEL="${GATEWAY_LAUNCHD_LABEL:-com.sanbo.ai-tool-functioncall.gateway.${PORT}}"
LAUNCHD_PLIST="$LOG_DIR/gateway-${PORT}.plist"
LAUNCHD_DOMAIN="gui/$(id -u)"
SCREEN_NAME="${GATEWAY_SCREEN_NAME:-ai-tool-functioncall-gateway-${PORT}}"
configured_value() {
  local key="$1" fallback="$2"
  ROOT_DIR="$ROOT_DIR" CONFIG_PATH="$CONFIG_PATH" CONFIG_KEY="$key" CONFIG_FALLBACK="$fallback" python3 - <<'PY'
import os, pathlib, sys
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

DOWNSTREAM_API_KEY="${DOWNSTREAM_API_KEY:-$(configured_value gateway.client_snippet_api_key '')}"
# Also check GATEWAY_DOWNSTREAM_KEY for Phase 1 compatibility
if [[ -z "$DOWNSTREAM_API_KEY" && -n "${GATEWAY_DOWNSTREAM_KEY:-}" ]]; then
  DOWNSTREAM_API_KEY="$GATEWAY_DOWNSTREAM_KEY"
fi
PUBLIC_BASE_URL="${GATEWAY_PUBLIC_BASE_URL:-http://127.0.0.1:${PORT}}"
STOP_EXISTING="${GATEWAY_STOP_EXISTING:-1}"
KILL_PORT_OWNER="${GATEWAY_KILL_PORT_OWNER:-1}"

export GATEWAY_CONFIG_PATH="$CONFIG_PATH"
export GATEWAY_RUNTIME_DIR="$LOG_DIR"
export GATEWAY_PUBLIC_EXPOSURE="${GATEWAY_PUBLIC_EXPOSURE:-auto}"
export GATEWAY_SQLITE_LOG_PATH="${GATEWAY_SQLITE_LOG_PATH:-$ROOT_DIR/gateway_log.sqlite3}"
# Real/test upstream URLs must be supplied by env or the ignored local
# .gateway_service.json; never bake a live address into the tracked script.
export UPSTREAM_BASE_URL="${UPSTREAM_BASE_URL:-}"
export UPSTREAM_API_KEY="${UPSTREAM_API_KEY:-}"
export UPSTREAM_MODEL="${UPSTREAM_MODEL:-mimo-v2.5-pro}"
export DOWNSTREAM_API_KEY
export GATEWAY_WORKSPACE_ROOT="${GATEWAY_WORKSPACE_ROOT:-$PWD}"
export GATEWAY_TOOL_MODE="${GATEWAY_TOOL_MODE:-orchestrate}"
export GATEWAY_TOOLS_ENABLED="${GATEWAY_TOOLS_ENABLED:-adapter}"
export GATEWAY_ALLOW_WRITE_TOOLS="${GATEWAY_ALLOW_WRITE_TOOLS:-1}"
export GATEWAY_ALLOW_SHELL_TOOLS="${GATEWAY_ALLOW_SHELL_TOOLS:-1}"
export GATEWAY_CONTEXT_ENABLED="${GATEWAY_CONTEXT_ENABLED:-1}"
export GATEWAY_CONTEXT_FANOUT_ENABLED="${GATEWAY_CONTEXT_FANOUT_ENABLED:-1}"
export GATEWAY_CONTEXT_MAX_INPUT_TOKENS="${GATEWAY_CONTEXT_MAX_INPUT_TOKENS:-1048576}"
export GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS="${GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS:-120000}"
export GATEWAY_CONTEXT_FANOUT_MAX_CHUNKS="${GATEWAY_CONTEXT_FANOUT_MAX_CHUNKS:-0}"
export GATEWAY_CONTEXT_FANOUT_MAX_WORKERS="${GATEWAY_CONTEXT_FANOUT_MAX_WORKERS:-4}"
export GATEWAY_MEMORY_ENABLED="${GATEWAY_MEMORY_ENABLED:-1}"
export GATEWAY_LOGGING_BACKEND="${GATEWAY_LOGGING_BACKEND:-sqlite}"
export GATEWAY_REQUEST_LOGGING="${GATEWAY_REQUEST_LOGGING:-1}"
export GATEWAY_MAX_CONCURRENT_REQUESTS="${GATEWAY_MAX_CONCURRENT_REQUESTS:-32}"
export UPSTREAM_MAX_CONCURRENCY="${UPSTREAM_MAX_CONCURRENCY:-32}"
export UPSTREAM_TIMEOUT="${UPSTREAM_TIMEOUT:-60}"
export UPSTREAM_MAX_INPUT_TOKENS="${UPSTREAM_MAX_INPUT_TOKENS:-1048576}"
export UPSTREAM_MAX_OUTPUT_TOKENS="${UPSTREAM_MAX_OUTPUT_TOKENS:-131072}"
export GATEWAY_CLIENT_CONTEXT_WINDOW="${GATEWAY_CLIENT_CONTEXT_WINDOW:-1048576}"
export GATEWAY_CLIENT_AUTO_COMPACT_TOKEN_LIMIT="${GATEWAY_CLIENT_AUTO_COMPACT_TOKEN_LIMIT:-943718}"
export GATEWAY_CLIENT_OUTPUT_TOKEN_LIMIT="${GATEWAY_CLIENT_OUTPUT_TOKEN_LIMIT:-131072}"
export GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT="${GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT:-48000}"
export GATEWAY_PUBLIC_BASE_URL="$PUBLIC_BASE_URL"
export GATEWAY_DOWNSTREAM_MODEL_ALIAS="${GATEWAY_DOWNSTREAM_MODEL_ALIAS:-$UPSTREAM_MODEL}"
export GATEWAY_REVIEW_MODEL_ALIAS="${GATEWAY_REVIEW_MODEL_ALIAS:-$UPSTREAM_MODEL}"
# Phase 1: Keys must be set via env vars - no hardcoded fallbacks
export GATEWAY_DOWNSTREAM_KEY="${GATEWAY_DOWNSTREAM_KEY:-}"
export GATEWAY_ADMIN_PASSWORD="${GATEWAY_ADMIN_PASSWORD:-}"
export GATEWAY_ADMIN_PASSWORD_HASH="${GATEWAY_ADMIN_PASSWORD_HASH:-}"

mkdir -p "$LOG_DIR"

port_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
  fi
}

kill_pid_gracefully() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
}

stop_existing_listener() {
  local pids cmd
  pids="$(port_pids | tr '\n' ' ' | xargs || true)"
  [[ -n "$pids" ]] || return 0
  if [[ "$STOP_EXISTING" != "1" && "$STOP_EXISTING" != "true" && "$STOP_EXISTING" != "yes" ]]; then
    echo "port $PORT already has listener(s): $pids" >&2
    return 48
  fi
  for pid in $pids; do
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$KILL_PORT_OWNER" == "1" || "$KILL_PORT_OWNER" == "true" || "$KILL_PORT_OWNER" == "yes" || "$cmd" == *"toolcall_gateway.py"* ]]; then
      echo "stopping listener on port $PORT: pid=$pid cmd=$cmd" >&2
      kill_pid_gracefully "$pid"
    else
      echo "refusing to stop non-gateway listener on port $PORT: pid=$pid cmd=$cmd" >&2
      return 48
    fi
  done
}

write_config_if_needed() {
  ROOT_DIR="$ROOT_DIR" CONFIG_PATH="$CONFIG_PATH" DOWNSTREAM_API_KEY="$DOWNSTREAM_API_KEY" python3 - <<'PY'
import datetime as dt
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ['ROOT_DIR'])
config_path = pathlib.Path(os.environ['CONFIG_PATH'])
force = os.environ.get('GATEWAY_FORCE_CONFIG', '0').lower() in {'1', 'true', 'yes'}
if config_path.exists() and not force:
    print(f'using existing gateway config: {config_path}', flush=True)
    raise SystemExit(0)

sys.path.insert(0, str(root))
import src.toolcall_gateway as gateway

cfg = gateway._default_config()
key = os.environ['DOWNSTREAM_API_KEY']
cfg['upstream'].update({
    'id': 'mimo-default',
    'name': 'Default Upstream',
    'base_url': os.environ.get('UPSTREAM_BASE_URL', '').rstrip('/'),
    'api_key': os.environ.get('UPSTREAM_API_KEY', ''),
    'model': os.environ.get('UPSTREAM_MODEL', 'mimo-v2.5-pro'),
    'protocol': os.environ.get('GATEWAY_UPSTREAM_PROTOCOL') or os.environ.get('UPSTREAM_PROTOCOL', 'openai_chat'),
    'tools_enabled': os.environ.get('GATEWAY_TOOLS_ENABLED', 'adapter'),
    'native_tools_verified': False,
    'use_for_coding': True,
    'max_input_tokens': int(os.environ.get('UPSTREAM_MAX_INPUT_TOKENS', '1048576')),
    'max_output_tokens': int(os.environ.get('UPSTREAM_MAX_OUTPUT_TOKENS', '131072')),
    'capabilities': {
        'supports_streaming': os.environ.get('UPSTREAM_SUPPORTS_STREAMING', '1').lower() in {'1','true','yes'},
        'supports_tools': os.environ.get('UPSTREAM_SUPPORTS_TOOLS', '0').lower() in {'1','true','yes'},
        'supports_function_calls': os.environ.get('UPSTREAM_SUPPORTS_FUNCTION_CALLS', '0').lower() in {'1','true','yes'},
        'supports_parallel_tool_calls': os.environ.get('UPSTREAM_SUPPORTS_PARALLEL_TOOL_CALLS', '0').lower() in {'1','true','yes'},
        'supports_vision': os.environ.get('UPSTREAM_SUPPORTS_VISION', '0').lower() in {'1','true','yes'},
        'supports_network': os.environ.get('UPSTREAM_SUPPORTS_NETWORK', '0').lower() in {'1','true','yes'},
        'supports_web_search': os.environ.get('UPSTREAM_SUPPORTS_WEB_SEARCH', '0').lower() in {'1','true','yes'},
        'supports_json_schema': os.environ.get('UPSTREAM_SUPPORTS_JSON_SCHEMA', '1').lower() in {'1','true','yes'},
    },
    'paths': {
        'models': os.environ.get('UPSTREAM_MODELS_PATH', '/v1/models'),
        'chat_completions': os.environ.get('UPSTREAM_CHAT_COMPLETIONS_PATH', '/v1/chat/completions'),
        'responses': os.environ.get('UPSTREAM_RESPONSES_PATH', '/v1/responses'),
        'messages': os.environ.get('UPSTREAM_MESSAGES_PATH', '/v1/messages'),
    },
})
cfg['upstream_profiles'] = [dict(cfg['upstream'])]
cfg['active_upstream'] = 'mimo-default'
# Only create downstream_keys if a key is provided - no hardcoded fallback
if key:
    cfg['downstream_keys'] = [{
        'name': 'local-client',
        'key_hash': gateway._hash_secret(key),
        'prefix': key[:8],
        'enabled': True,
        'protocols': ['models', 'chat_completions', 'responses', 'messages', 'direct_tools'],
        'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    }]
else:
    cfg['downstream_keys'] = []
cfg.setdefault('gateway', {})['client_snippet_api_key'] = key
cfg['gateway']['allow_write_tools'] = os.environ.get('GATEWAY_ALLOW_WRITE_TOOLS', '1').lower() in {'1','true','yes'}
cfg['gateway']['allow_shell_tools'] = os.environ.get('GATEWAY_ALLOW_SHELL_TOOLS', '1').lower() in {'1','true','yes'}
cfg['gateway']['workspace_root'] = os.environ.get('GATEWAY_WORKSPACE_ROOT', str(root))
cfg['gateway']['client_context_window'] = int(os.environ.get('GATEWAY_CLIENT_CONTEXT_WINDOW', '1048576'))
cfg['gateway']['client_auto_compact_token_limit'] = int(os.environ.get('GATEWAY_CLIENT_AUTO_COMPACT_TOKEN_LIMIT', '943718'))
cfg['gateway']['client_output_token_limit'] = int(os.environ.get('GATEWAY_CLIENT_OUTPUT_TOKEN_LIMIT', '131072'))
cfg['gateway']['text_tool_adapter_compact_token_limit'] = int(os.environ.get('GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT', '48000'))
cfg.setdefault('context', {})['max_input_tokens'] = int(os.environ.get('GATEWAY_CONTEXT_MAX_INPUT_TOKENS', '1048576'))
cfg.setdefault('context', {})['fanout_chunk_tokens'] = int(os.environ.get('GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS', '120000'))
cfg['gateway']['sqlite_log_path'] = os.environ.get('GATEWAY_SQLITE_LOG_PATH', str(root / 'gateway_log.sqlite3'))
# Use the canonical fail-closed encrypted/atomic writer. Direct write_text here
# previously persisted upstream and downstream credentials in plaintext.
gateway.save_config(gateway._sync_active_upstream(cfg))
print(f'wrote gateway config: {config_path}', flush=True)
PY
}

write_launchd_plist() {
  cat > "$LAUNCHD_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LAUNCHD_LABEL}</string>
  <key>WorkingDirectory</key><string>${ROOT_DIR}</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v python3)</string>
    <string>${ROOT_DIR}/src/toolcall_gateway.py</string>
    <string>--host</string><string>${HOST}</string>
    <string>--port</string><string>${PORT}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>GATEWAY_CONFIG_PATH</key><string>${GATEWAY_CONFIG_PATH}</string>
    <key>GATEWAY_RUNTIME_DIR</key><string>${GATEWAY_RUNTIME_DIR}</string>
    <key>GATEWAY_SQLITE_LOG_PATH</key><string>${GATEWAY_SQLITE_LOG_PATH}</string>
    <key>UPSTREAM_BASE_URL</key><string>${UPSTREAM_BASE_URL}</string>
    <key>UPSTREAM_MODEL</key><string>${UPSTREAM_MODEL}</string>
    <key>UPSTREAM_MAX_INPUT_TOKENS</key><string>${UPSTREAM_MAX_INPUT_TOKENS}</string>
    <key>UPSTREAM_MAX_OUTPUT_TOKENS</key><string>${UPSTREAM_MAX_OUTPUT_TOKENS}</string>
    <key>GATEWAY_WORKSPACE_ROOT</key><string>${GATEWAY_WORKSPACE_ROOT}</string>
    <key>GATEWAY_TOOL_MODE</key><string>${GATEWAY_TOOL_MODE}</string>
    <key>GATEWAY_TOOLS_ENABLED</key><string>${GATEWAY_TOOLS_ENABLED}</string>
    <key>GATEWAY_ALLOW_WRITE_TOOLS</key><string>${GATEWAY_ALLOW_WRITE_TOOLS}</string>
    <key>GATEWAY_ALLOW_SHELL_TOOLS</key><string>${GATEWAY_ALLOW_SHELL_TOOLS}</string>
    <key>GATEWAY_CONTEXT_ENABLED</key><string>${GATEWAY_CONTEXT_ENABLED}</string>
    <key>GATEWAY_CONTEXT_FANOUT_ENABLED</key><string>${GATEWAY_CONTEXT_FANOUT_ENABLED}</string>
    <key>GATEWAY_CONTEXT_MAX_INPUT_TOKENS</key><string>${GATEWAY_CONTEXT_MAX_INPUT_TOKENS}</string>
    <key>GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS</key><string>${GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS}</string>
    <key>GATEWAY_CLIENT_CONTEXT_WINDOW</key><string>${GATEWAY_CLIENT_CONTEXT_WINDOW}</string>
    <key>GATEWAY_CLIENT_AUTO_COMPACT_TOKEN_LIMIT</key><string>${GATEWAY_CLIENT_AUTO_COMPACT_TOKEN_LIMIT}</string>
    <key>GATEWAY_CLIENT_OUTPUT_TOKEN_LIMIT</key><string>${GATEWAY_CLIENT_OUTPUT_TOKEN_LIMIT}</string>
    <key>GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT</key><string>${GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT}</string>
    <key>GATEWAY_MEMORY_ENABLED</key><string>${GATEWAY_MEMORY_ENABLED}</string>
    <key>GATEWAY_LOGGING_BACKEND</key><string>${GATEWAY_LOGGING_BACKEND}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${LOG_FILE}</string>
  <key>StandardErrorPath</key><string>${LOG_FILE}</string>
</dict></plist>
PLIST
}

start_launchd() {
  write_config_if_needed
  write_launchd_plist
  launchctl bootout "$LAUNCHD_DOMAIN" "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
  stop_existing_listener
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$LAUNCHD_PLIST"
  launchctl enable "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL" >/dev/null 2>&1 || true
  launchctl kickstart -k "$LAUNCHD_DOMAIN/$LAUNCHD_LABEL" >/dev/null 2>&1 || true
  if ! wait_health; then
    echo "gateway did not become healthy; see $LOG_FILE" >&2
    tail -n 80 "$LOG_FILE" >&2 || true
    exit 1
  fi
  print_endpoints
  curl -fsS "http://127.0.0.1:${PORT}/healthz" || true
  echo
}


wait_health() {
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  return 1
}

print_endpoints() {
  cat <<EOF
Gateway:
  local:  http://127.0.0.1:${PORT}
  bind:   http://${HOST}:${PORT}
  ui:     http://127.0.0.1:${PORT}/ui
  config: http://127.0.0.1:${PORT}/client-config
Auth:
  API key: ${DOWNSTREAM_API_KEY:0:4}*** (from env or local config)
  Admin:   admin / admin (change it in UI before production)
Files:
  config:  ${CONFIG_PATH}
  sqlite:  ${GATEWAY_SQLITE_LOG_PATH}
  log:     ${LOG_FILE}
EOF
}

start_foreground() {
  write_config_if_needed
  stop_existing_listener
  print_endpoints
  exec python3 "$ROOT_DIR/src/toolcall_gateway.py" --host "$HOST" --port "$PORT"
}

start_background() {
  if [[ "${GATEWAY_START_METHOD:-screen}" == "launchd" ]] && command -v launchctl >/dev/null 2>&1 && [[ "$(uname -s)" == "Darwin" ]]; then
    start_launchd
    return
  fi
  write_config_if_needed
  stop_existing_listener
  if command -v screen >/dev/null 2>&1 && [[ "${GATEWAY_START_METHOD:-screen}" != "nohup" ]]; then
    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
    local cmd
    printf -v cmd 'cd %q && exec python3 %q --host %q --port %q >>%q 2>&1' "$ROOT_DIR" "$ROOT_DIR/src/toolcall_gateway.py" "$HOST" "$PORT" "$LOG_FILE"
    screen -dmS "$SCREEN_NAME" bash -lc "$cmd"
    sleep 0.2
    local pids
    pids="$(port_pids | head -1 || true)"
    [[ -n "$pids" ]] && echo "$pids" > "$PID_FILE"
  else
    nohup python3 "$ROOT_DIR/src/toolcall_gateway.py" --host "$HOST" --port "$PORT" </dev/null >>"$LOG_FILE" 2>&1 &
    local gateway_pid=$!
    disown "$gateway_pid" 2>/dev/null || true
    echo "$gateway_pid" > "$PID_FILE"
  fi
  if ! wait_health; then
    echo "gateway did not become healthy; see $LOG_FILE" >&2
    tail -n 80 "$LOG_FILE" >&2 || true
    exit 1
  fi
  print_endpoints
  curl -fsS "http://127.0.0.1:${PORT}/healthz" || true
  echo
}


stop_service() {
  if command -v launchctl >/dev/null 2>&1; then
    launchctl bootout "$LAUNCHD_DOMAIN" "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
  fi
  if command -v screen >/dev/null 2>&1; then
    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
  fi
  if [[ -f "$PID_FILE" ]]; then
    kill_pid_gracefully "$(cat "$PID_FILE")"
    rm -f "$PID_FILE"
  fi
  stop_existing_listener || true
}

status_service() {
  print_endpoints
  echo "Health:"
  curl -fsS "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo "not running"
  echo
  local pids
  pids="$(port_pids | tr '\n' ' ' | xargs || true)"
  if [[ -n "$pids" ]]; then ps -p $pids -o pid,etime,command; fi
  if command -v screen >/dev/null 2>&1; then
    screen -ls | grep -F "$SCREEN_NAME" || true
  fi
}

verify_all() {
  echo "== 1/5 compile + unit tests =="
  python3 -m py_compile "$ROOT_DIR/src/toolcall_gateway.py" "$ROOT_DIR/src/gateway_app.py" "$ROOT_DIR/src/gateway_builtin_tools.py" "$ROOT_DIR/tests/test_gateway.py" "$ROOT_DIR/tests/integration/"*.py
  local test_dir
  test_dir="$(mktemp -d)"
  env \
    -u UPSTREAM_BASE_URL -u UPSTREAM_API_KEY -u UPSTREAM_MODEL \
    -u UPSTREAM_MAX_CONCURRENCY -u UPSTREAM_TIMEOUT \
    -u UPSTREAM_MAX_INPUT_TOKENS -u UPSTREAM_MAX_OUTPUT_TOKENS \
    -u DOWNSTREAM_API_KEY -u GATEWAY_DOWNSTREAM_KEY \
    -u GATEWAY_TOOLS_ENABLED -u GATEWAY_TOOL_MODE \
    -u GATEWAY_ALLOW_WRITE_TOOLS -u GATEWAY_ALLOW_SHELL_TOOLS \
    -u GATEWAY_CONTEXT_ENABLED -u GATEWAY_CONTEXT_FANOUT_ENABLED \
    -u GATEWAY_CONTEXT_MAX_INPUT_TOKENS -u GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS \
    -u GATEWAY_CONTEXT_FANOUT_MAX_CHUNKS -u GATEWAY_CONTEXT_FANOUT_MAX_WORKERS \
    -u GATEWAY_UPSTREAM_STREAM_AGGREGATE -u GATEWAY_MEMORY_ENABLED \
    -u GATEWAY_LOGGING_BACKEND -u GATEWAY_REQUEST_LOGGING \
    -u GATEWAY_MAX_CONCURRENT_REQUESTS -u GATEWAY_WORKSPACE_ROOT \
    -u GATEWAY_SKILLS_DIRS -u GATEWAY_PUBLIC_BASE_URL \
    -u GATEWAY_DOWNSTREAM_MODEL_ALIAS -u GATEWAY_REVIEW_MODEL_ALIAS \
    -u GATEWAY_ADMIN_PASSWORD -u GATEWAY_ADMIN_PASSWORD_HASH \
    -u GATEWAY_CLIENT_CONTEXT_WINDOW -u GATEWAY_CLIENT_AUTO_COMPACT_TOKEN_LIMIT \
    -u GATEWAY_CLIENT_OUTPUT_TOKEN_LIMIT -u GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT \
    -u GATEWAY_MAX_REQUEST_BODY_BYTES -u GATEWAY_MAX_LOG_PAYLOAD_CHARS \
    -u GATEWAY_MAX_TOOL_ROUNDS -u GATEWAY_TOOL_MAX_RETRIES \
    -u GATEWAY_READ_DEFAULT_LIMIT -u GATEWAY_ALLOW_FILE_LOGGING \
    PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    GATEWAY_CONFIG_PATH="$test_dir/config.json" \
    GATEWAY_SQLITE_LOG_PATH="$test_dir/gateway.sqlite3" \
    python3 -m unittest discover -s "$ROOT_DIR/tests" -v
  rm -rf "$test_dir"
  if ! curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then start_background >/dev/null; fi
  local effective_key
  effective_key="$(configured_value gateway.client_snippet_api_key "${DOWNSTREAM_API_KEY:-local-gateway-key}")"
  echo "== 2/5 CORE TOOL ACCEPTANCE =="
  "$ROOT_DIR/tests/integration/tool_acceptance.py" --base-url "http://127.0.0.1:${PORT}" --key "$effective_key"
  echo "== 3/5 security/auth guardrails =="
  "$ROOT_DIR/tests/integration/security_gateway_checks.py" --base-url "http://127.0.0.1:${PORT}" --key "$effective_key"
  echo "== 4/5 concurrency/performance smoke =="
  "$ROOT_DIR/tests/integration/stress_gateway_concurrency.py" --base-url "http://127.0.0.1:${PORT}" --key "$effective_key" --workers "${GATEWAY_VERIFY_WORKERS:-16}" --direct-tool-requests "${GATEWAY_VERIFY_DIRECT_REQUESTS:-32}" --model-requests "${GATEWAY_VERIFY_MODEL_REQUESTS:-1}"
  echo "== 5/5 Claude/Codex project-scope smoke =="
  local cli_args=()
  if [[ "${GATEWAY_VERIFY_REQUIRE_CLI:-0}" == "1" || "${GATEWAY_VERIFY_REQUIRE_CLI:-0}" == "true" || "${GATEWAY_VERIFY_REQUIRE_CLI:-0}" == "yes" ]]; then
    cli_args+=(--require-claude --require-codex)
  fi
  "$ROOT_DIR/tests/integration/project_scope_cli_smoke.py" "${cli_args[@]}"
}

case "$ACTION" in
  start) start_background ;;
  foreground|run) start_foreground ;;
  stop) stop_service ;;
  restart) stop_service; start_background ;;
  status) status_service ;;
  logs) tail -n "${2:-160}" -f "$LOG_FILE" ;;
  config) write_config_if_needed; echo "$CONFIG_PATH" ;;
  verify) verify_all ;;
  *) echo "usage: $0 [start|foreground|stop|restart|status|logs|config|verify]" >&2; exit 2 ;;
esac
