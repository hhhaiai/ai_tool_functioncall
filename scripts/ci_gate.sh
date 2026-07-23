#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

python3 -m compileall -q src tests
python3 -m json.tool gateway.config.json >/dev/null
python3 - <<'PY'
from pathlib import Path
import yaml

paths = [Path("gateway.config.yaml"), Path("mcp_defaults.yaml")]
paths.extend(sorted(Path(".github/workflows").glob("*.yml")))
paths.extend(sorted(Path(".github/workflows").glob("*.yaml")))
for path in paths:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a YAML mapping")
PY

python3 -m ruff check src tests
python3 -m mypy \
  src/gateway_sqlite.py \
  src/gateway_sqlite_compact.py \
  src/gateway_admission.py \
  src/gateway_admin_client_mutations.py \
  src/gateway_admin_catalog_mutations.py \
  src/gateway_admin_config_mutations.py \
  src/gateway_admin_connector_mutations.py \
  src/gateway_admin_security.py \
  src/gateway_admin_operations.py \
  src/gateway_admin_api.py \
  src/gateway_assistants.py \
  src/gateway_http_auth.py \
  src/gateway_http_io.py \
  src/gateway_llm.py \
  src/gateway_request_admission.py \
  src/gateway_observability.py \
  src/gateway_upstream_pool.py
python3 -m bandit -q -lll \
  src/gateway_sqlite.py \
  src/gateway_sqlite_compact.py \
  src/gateway_admission.py \
  src/gateway_observability.py \
  src/gateway_admin_client_mutations.py \
  src/gateway_admin_catalog_mutations.py \
  src/gateway_admin_config_mutations.py \
  src/gateway_admin_connector_mutations.py \
  src/gateway_admin_security.py \
  src/gateway_admin_api.py \
  src/gateway_assistants.py \
  src/gateway_http_auth.py \
  src/gateway_http_io.py \
  src/gateway_http_security.py \
  src/gateway_llm.py \
  src/gateway_sandbox.py \
  src/gateway_sandbox_worker.py \
  src/gateway_upstream_pool.py
python3 -m pip check
python3 -m pip_audit -r requirements.txt

python3 -m pytest -q
git diff --check

tracked_forbidden=$(git ls-files | python3 -c '
import re, sys
for raw in sys.stdin:
    path = raw.strip()
    if path == ".env.example":
        continue
    if re.search(r"(^|/)(\.env(?:\..*)?|.*\.sqlite3(?:-.*)?|.*\.(?:pem|key))$", path):
        print(path)
')
if [[ -n "$tracked_forbidden" ]]; then
  printf 'Forbidden runtime/secret-like files are tracked:\n%s\n' "$tracked_forbidden" >&2
  exit 1
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose -f docker-compose.yml config -q
  UPSTREAM_BASE_URL=http://127.0.0.1:1 \
  UPSTREAM_API_KEY=dummy-ci-only \
  UPSTREAM_MODEL=dummy-model \
  GATEWAY_ADMIN_PASSWORD=dummy-admin-ci-only \
  GATEWAY_DOWNSTREAM_KEY=dummy-downstream-ci-only \
    docker compose -f docker-compose.prod.yml config -q
  docker build -t ai-gateway-ci-gate .
  docker image rm ai-gateway-ci-gate >/dev/null
fi
