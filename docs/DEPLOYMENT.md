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

### Step 2: Generate SSL Certificates (Optional)

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

1. Uncomment the SSL lines
2. Update `server_name` with your domain
3. Configure SSL certificate paths

### Step 4: Deploy

```bash
# Build and start; compose fails fast if required secrets are missing
./scripts/deploy.sh production up

# Check status
./scripts/deploy.sh production status

# View logs
./scripts/deploy.sh production logs
```

### Step 5: Configure Firewall

```bash
# Allow HTTP and HTTPS
ufw allow 80/tcp
ufw allow 443/tcp

# Enable firewall
ufw enable
```

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
| `GATEWAY_TOOL_MODE` | Tool mode | orchestrate |
| `GATEWAY_ALLOW_WRITE_TOOLS` | Enable workspace write/edit tools | `0` |
| `GATEWAY_ALLOW_SHELL_TOOLS` | Enable shell/code execution tools | `0` |
| `GATEWAY_CONTEXT_ENABLED` | Enable context management | 1 |
| `GATEWAY_MEMORY_ENABLED` | Enable memory system | 1 |

### Configuration File

The gateway uses `.gateway_service.json` for runtime configuration in local/script mode. Docker production should pass secrets through environment variables and keep `.gateway_service.json`, SQLite logs, and `nginx/ssl/` out of Git.

## Monitoring

### Health Check

```bash
curl http://localhost:8885/healthz
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
3. **Least privilege tools**: Keep `GATEWAY_ALLOW_WRITE_TOOLS=0` and `GATEWAY_ALLOW_SHELL_TOOLS=0` unless the deployment is a trusted coding-agent workspace
4. **Use HTTPS**: Configure SSL certificates for production
5. **Restrict access**: Use firewall rules to limit access
6. **API keys**: Use `GATEWAY_DOWNSTREAM_KEY` for client authentication
7. **Regular updates**: Keep Docker images updated
