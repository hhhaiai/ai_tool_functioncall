# Gateway Deployment Guide

## Quick Start (Development)

```bash
# 1. Clone and configure
git clone <repo>
cd ai_tool_functioncall
cp .env.example .env
# Edit .env with your upstream API settings

# 2. Start with Docker
./scripts/deploy.sh development up

# 3. Verify
curl http://localhost:8885/healthz
```

## Production Deployment

### Prerequisites

- Docker and Docker Compose installed
- Domain name (for SSL)
- SSL certificates (or use self-signed for testing)

### Step 1: Configure Environment

```bash
cp .env.example .env
# Edit .env with production values:
# - Set secure GATEWAY_ADMIN_PASSWORD
# - Set GATEWAY_DOWNSTREAM_KEY for API authentication
# - Configure UPSTREAM_BASE_URL, UPSTREAM_API_KEY, UPSTREAM_MODEL
```

### Step 2: Install SSL Certificates (Required for Production)

For development/testing with self-signed certificates:

```bash
./scripts/generate-ssl.sh your-domain.com
```

For production, use Let's Encrypt:

```bash
# Install certbot
apt-get install certbot

# Generate certificates
certbot certonly --standalone -d your-domain.com

# Copy certificates
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem nginx/ssl/cert.pem
cp /etc/letsencrypt/live/your-domain.com/privkey.pem nginx/ssl/key.pem
```

### Step 3: Configure Nginx

Edit `nginx/nginx.conf`:

TLS is enabled by default in the production Nginx configuration. Before
starting Compose, place the certificate chain at `nginx/ssl/cert.pem` and the
private key at `nginx/ssl/key.pem`, then update `server_name` if desired.
Port 80 only redirects to HTTPS; production API/Admin traffic is never served
in plaintext by the bundled ingress.

### Step 4: Deploy

```bash
# Build and start; compose fails fast if required secrets are missing
./scripts/deploy.sh production up

# Check status
./scripts/deploy.sh production status

# View logs
./scripts/deploy.sh production logs
```

Production Compose binds the raw Gateway port to server loopback only:
`127.0.0.1:${GATEWAY_PORT:-8885}`. Remote Codex/Claude Code clients must use
the Nginx HTTPS endpoint. This prevents bypassing TLS, rate limiting, and
the streaming proxy configuration. Both `/v1/` and `/anthropic/` have SSE
buffering disabled for protocol-level tool calls.

The ordinary `docker-compose.yml` is also loopback-only by default. It sets
`GATEWAY_PUBLIC_EXPOSURE=private` because the process listens on `0.0.0.0`
inside the container while Docker publishes the host port only on
`127.0.0.1`. Do not keep that assertion if you change the publication to an
external interface.

For direct process launches, the default `GATEWAY_PUBLIC_EXPOSURE=auto`
treats a non-loopback listener as external. External startup fails before
binding unless the effective decrypted configuration contains a non-default
Admin password and at least one enabled downstream API key. Use `private`
only when loopback publication, a firewall, or private ingress actually
provides that boundary; production Compose declares `external` explicitly.

Browser CORS is disabled by default. Enable only exact origins when a browser
application must call the API:

```bash
GATEWAY_CORS_ENABLED=1
GATEWAY_CORS_ALLOWED_ORIGINS=https://console.example.com,https://ops.example.com
```

Wildcard origins are not supported. JSON, text, error, OPTIONS, and SSE
responses share the same allowlist. State-changing Admin requests retain an
additional same-origin Origin/Referer check.

### Step 5: Configure Firewall

```bash
# Allow HTTP and HTTPS
ufw allow 80/tcp
ufw allow 443/tcp

# Enable firewall
ufw enable
```

### Step 6: Verify the External Function-Call Boundary

Run this from outside the Gateway process/container, using the same base URL
and downstream key configured for Codex/Claude Code:

```bash
python3 tests/integration/server_gateway_external_smoke.py \
  --base-url https://gateway.example.com \
  --key "$GATEWAY_DOWNSTREAM_KEY" \
  --model "$UPSTREAM_MODEL"
```

The smoke requires all of the following: unauthenticated API requests return
401; OpenAI Chat returns `tool_calls`; OpenAI Responses returns
`function_call`; Anthropic Messages returns `tool_use`; Gateway-owned
`calculator` executes in the service; user-side `Read` is rejected by the
direct endpoint and remains owned by the downstream Codex/Claude Code client.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `UPSTREAM_BASE_URL` | Upstream API URL | (required) |
| `UPSTREAM_API_KEY` | Upstream API key | (required) |
| `UPSTREAM_MODEL` | Default model | (required) |
| `GATEWAY_UPSTREAM_PROTOCOL` | Upstream protocol (`openai_chat`, `openai_responses`, `anthropic_messages`) | `openai_chat` |
| `UPSTREAM_PROTOCOL` | Legacy upstream protocol env, used only when `GATEWAY_UPSTREAM_PROTOCOL` is unset | `openai_chat` |
| `GATEWAY_PORT` | Gateway port | 8885 |
| `GATEWAY_ADMIN_PASSWORD` | Admin password; production compose requires it | `admin` in dev only |
| `GATEWAY_DOWNSTREAM_KEY` | API key for clients; required for production | (required in production) |
| `GATEWAY_PUBLIC_EXPOSURE` | Listener contract: `auto`, `private`, or `external` | `auto` |
| `GATEWAY_CORS_ENABLED` | Enable browser cross-origin API access | `0` |
| `GATEWAY_CORS_ALLOWED_ORIGINS` | Comma-separated exact HTTP(S) origins | empty |
| `GATEWAY_TOOL_MODE` | Tool mode | orchestrate |
| `GATEWAY_ALLOW_WRITE_TOOLS` | Enable workspace write/edit tools | `0` |
| `GATEWAY_ALLOW_SHELL_TOOLS` | Enable shell/code execution tools | `0` |
| `GATEWAY_EXECUTE_USER_SIDE_TOOLS` | Execute user-side filesystem/shell/GUI/local-agent tools on the Gateway host instead of delegating to client | `0` |
| `GATEWAY_ADMIN_SKILLS_ROOT` | Writable service-wide Admin Skill catalog; project Skills retain precedence | `/app/data/skills` in Compose |
| `GATEWAY_TOOL_ENV_ALLOWLIST` | Extra comma-separated environment names inherited by trusted tool subprocesses | empty |
| `GATEWAY_SANDBOX_ISOLATION_BACKEND` | `auto`, `macos_sandbox`, `linux_landlock`, or resource-only `rlimit` | `auto` |
| `GATEWAY_SANDBOX_READ_POLICY` | `system_and_workspace` or compatibility mode `inherited` | OS backend: restricted; `rlimit`: inherited |
| `GATEWAY_SANDBOX_DENY_READ_PATHS` | Extra comma-separated absolute paths hidden from executable tools | empty |
| `GATEWAY_SANDBOX_TENANT_ROOT` | Parent whose top-level children are isolated tenant/workspace roots | `/app/workspace` in Compose |
| `GATEWAY_SANDBOX_NETWORK_POLICY` | Default Shell/code/exec network policy: `inherited` or `deny` | `inherited` |
| `GATEWAY_SANDBOX_CPU_SECONDS` | CPU-seconds rlimit; blank derives `wall timeout + 1` for short jobs and stays unlimited for long-lived exec/MCP | blank |
| `GATEWAY_SANDBOX_MEMORY_BYTES` | Address-space rlimit when supported; opt-in because platform behavior differs | empty |
| `GATEWAY_SANDBOX_MAX_PROCESSES` | Per-user process-count rlimit when supported; opt-in because platform behavior differs | empty |
| `GATEWAY_SANDBOX_MAX_OPEN_FILES` | Open-file rlimit for executable workers | `256` |
| `GATEWAY_SANDBOX_MAX_FILE_BYTES` | Maximum size of a file created by an executable worker | `67108864` |
| `GATEWAY_CONTEXT_ENABLED` | Enable context management | 1 |
| `GATEWAY_MEMORY_ENABLED` | Enable memory system | 1 |
| `GATEWAY_MAX_CONCURRENT_REQUESTS` | Shared downstream request limit | `32` |
| `GATEWAY_CONCURRENCY_BACKEND` | Request admission backend: `sqlite` or process-local `memory` | `sqlite` |
| `GATEWAY_CONCURRENCY_DB_PATH` | Shared admission lease database | runtime `admission.sqlite3` |
| `GATEWAY_CONCURRENCY_FALLBACK_BACKEND` | Backend used when SQLite admission fails; `none` fails closed | `none` |
| `GATEWAY_CONCURRENCY_LEASE_TTL_SECONDS` | Crash-recovery lease TTL | `120` |
| `GATEWAY_CONCURRENCY_HEARTBEAT_SECONDS` | Active request lease heartbeat interval | `30` |
| `GATEWAY_CONCURRENCY_QUEUE_TIMEOUT` | Wait before returning concurrency 429 | `5` |

### Configuration File

The gateway uses `.gateway_service.json` for runtime configuration in local/script mode. Docker production should pass secrets through environment variables and keep `.gateway_service.json`, SQLite logs, and `nginx/ssl/` out of Git.

## Monitoring

### Health Check

```bash
curl http://localhost:8885/livez   # process is alive
curl http://localhost:8885/readyz  # initialized and accepting traffic
```

Response:
```json
{
  "ok": true,
  "mode": "orchestrate",
  "fake_prompt_tools": false,
  "supported_paths": [...],
  "builtin_tool_count": 67
}
```

### Statistics

Access statistics at:
- `/admin/stats.json` - Request and tool statistics
- `/admin/requests.json` - Recent requests
- `/admin/failures.json` - Tool failures
- `/admin/memories.json` - Conversation memories
- `/admin/storage.json` - Read-only SQLite size, integrity, auto-vacuum, and compaction eligibility preflight
- `/admin/metrics` - Prometheus counters/gauges plus bounded HTTP, tool, upstream, and first-stream-event latency histograms
- `/admin/traces.json` - Authenticated bounded in-memory trace ring (default query limit 100, maximum 1000)

Every parsed HTTP request receives an `x-request-id` response header. A valid
caller-supplied `x-request-id` is preserved for correlation; otherwise the
Gateway generates a random `req_...` value. Request, tool, and upstream spans
share this ID through request-local context.

Metrics deliberately use only bounded labels: normalized route, method,
status class, protocol, stream flag, built-in tool name or generic tool class,
outcome, and normalized failure type. Raw URLs, upstream hosts, tenant/user
names, prompts, tool arguments, API keys, and arbitrary MCP/action names are
not metric labels. Trace attributes use an explicit allowlist and the trace
ring is process-local, memory-only, and bounded to 1000 entries.

### Legacy SQLite compaction

Databases created before incremental auto-vacuum was enabled remain in
`auto_vacuum=0` mode. Background maintenance deliberately does not run a full
`VACUUM`, because it can require substantial temporary disk space and hold an
exclusive database lock.

First inspect the database. This command is read-only:

```bash
python3 -m src.gateway_sqlite_compact --database gateway_log.sqlite3
```

To migrate and compact it, stop every Gateway process/container that can use
the database, take a backup, and then run:

```bash
python3 -m src.gateway_sqlite_compact \
  --database gateway_log.sqlite3 \
  --execute \
  --confirm-gateway-stopped
```

Execution refuses to proceed when WAL/SHM/journal artifacts exist, disk
headroom is insufficient, another compactor holds the advisory lock, SQLite
cannot obtain exclusive access, the source changes during compaction, or the
candidate fails integrity/schema/table-count verification. The candidate is
created and validated beside the source before an atomic replacement. The
installed file is mode `0600` and is re-opened for final verification.

There is intentionally no online HTTP endpoint that executes this operation.
The authenticated Admin endpoint exposes preflight information only.

### Logs

View logs:

```bash
# Docker logs
./scripts/deploy.sh production logs

# SQLite database
sqlite3 gateway_log.sqlite3 "SELECT * FROM request_logs ORDER BY id DESC LIMIT 10;"
```

## Backup

### Backup Configuration

```bash
# Backup configuration and data
tar -czf gateway-backup-$(date +%Y%m%d).tar.gz \
    .gateway_service.json \
    gateway_log.sqlite3 \
    nginx/ssl/
```

### Restore Configuration

```bash
# Extract backup
tar -xzf gateway-backup-YYYYMMDD.tar.gz

# Restart gateway
./scripts/deploy.sh production restart
```

## Troubleshooting

### Gateway Not Starting

Check logs:
```bash
./scripts/deploy.sh production logs
```

Common issues:
- Port already in use
- Invalid configuration
- Missing upstream API credentials

### SSL Issues

Regenerate certificates:
```bash
./scripts/generate-ssl.sh your-domain.com
```

### Performance Issues

Check resource usage:
```bash
docker stats
```

Adjust limits in `docker-compose.prod.yml`:
```yaml
deploy:
  resources:
    limits:
      cpus: '4'
      memory: 4G
```

## Security Considerations

1. **Change default passwords**: Always change `GATEWAY_ADMIN_PASSWORD`
2. **Require client auth**: Always set `GATEWAY_DOWNSTREAM_KEY` in production
3. **Least privilege tools**: Keep `GATEWAY_ALLOW_WRITE_TOOLS=0`, `GATEWAY_ALLOW_SHELL_TOOLS=0`, and `GATEWAY_EXECUTE_USER_SIDE_TOOLS=0` unless the deployment is a trusted local coding-agent workspace where Gateway and client intentionally share the same machine/workspace
4. **Use HTTPS**: Configure SSL certificates for production
5. **Restrict access**: Use firewall rules to limit access
6. **API keys**: Use `GATEWAY_DOWNSTREAM_KEY` for client authentication
7. **Regular updates**: Keep Docker images updated

Gateway-owned Shell, code, Git, apply_patch, exec-session, and MCP processes do
not inherit the complete service environment. The default positive allowlist
keeps basic process variables such as `PATH`, `HOME`, locale, terminal, and
temporary-directory settings while excluding API keys, tokens, passwords,
proxy credentials, and Gateway configuration variables. An MCP server receives
only that base plus its explicit administrator-configured `server.env`.

If a trusted local executable truly requires another inherited variable, add
only its name to `GATEWAY_TOOL_ENV_ALLOWLIST`. Avoid adding broad credential
variables; prefer an MCP-specific `server.env` entry so the secret is scoped to
one server instead of every executable tool.

Executable tools are launched through a separate versioned worker process. On
supported POSIX platforms the worker applies CPU, open-file, and output-file
rlimits before replacing itself with the real command, so the multithreaded
Gateway does not rely on `preexec_fn`. Memory and process-count limits are
available as explicit opt-ins. A requested limit that the host cannot apply is
a fail-closed sandbox setup error; it never silently falls back to an
unrestricted command. Timeout, explicit cancellation, shutdown, and session
cleanup terminate the complete worker process group.

With `GATEWAY_SANDBOX_ISOLATION_BACKEND=auto`, macOS uses the built-in
`sandbox-exec` profile and Linux uses unprivileged Landlock. Both restrict
writes to the declared workspace scope plus the bounded system temporary
directory. Linux network-deny jobs additionally install a libseccomp filter
that rejects new sockets; macOS omits network permissions from the sandbox
profile. Git and apply_patch always request network denial. MCP defaults to its
configured working directory with inherited networking because many MCP
connectors are network services; an individual server may set
`network_policy: deny` and explicit `writable_paths`.

On Linux, the default `system_and_workspace` read policy is a Landlock
allowlist for operating-system runtime files, PATH/Python dependencies,
absolute administrator-supplied executable arguments, the declared workspace,
and temporary storage. It does not grant a blanket read of HOME or the Gateway
runtime/config volume. If a sensitive file is intentionally placed inside the
declared workspace, the workspace rule necessarily makes it readable; keep
Gateway credentials and runtime state on the separate `/app/data` volume.

Current macOS `sandbox-exec` cannot reliably launch normal commands under the
same comprehensive read allowlist. The macOS backend therefore allows normal
runtime reads but adds higher-priority deny rules for Gateway config/log/runtime
paths, common HOME credential locations (`.ssh`, cloud/Kubernetes credentials,
Docker config, netrc/npm/pypi credentials), and
`GATEWAY_SANDBOX_DENY_READ_PATHS`. This is a targeted secret boundary, not a
claim that every unrelated HOME file is hidden. Set `inherited` only for a
trusted executable with a documented need; the resource-only `rlimit` backend
cannot enforce restricted reads and rejects that combination.

When `GATEWAY_SANDBOX_TENANT_ROOT` is set and a request workspace is one of its
top-level children, the job also denies reads from every sibling tenant root.
Writes are already restricted to the active workspace. Tenant-root enumeration
failure is a sandbox setup error rather than a silent cross-tenant fallback.

The Linux backend does not require `SYS_ADMIN`, privileged containers, a Docker
socket, or unprivileged user namespaces. If Landlock or libseccomp cannot apply
a requested policy, the worker returns `sandbox_setup_failed`; it does not
silently run the command with weaker isolation. `rlimit` is an explicit
compatibility backend and rejects `network_policy=deny`.

`apply_patch` receives an additional transactional boundary: it runs against a
temporary overlay containing only declared targets, emits a hash-backed diff,
rejects undeclared files/directories/symlinks, checks that the original files
did not change during execution, and then commits with atomic per-file replace
plus rollback on partial commit failure.

These worker limits are still not complete malicious-code isolation. Shell,
code, long-lived exec, and MCP processes share the configured workspace and
network namespace; a hostile executable can attempt direct absolute-path or
network access outside its declared policy. Keep Shell/write/user-side
execution disabled for untrusted remote clients until OS/container filesystem
and network isolation are enabled for those paths.
