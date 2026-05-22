FROM python:3.11-slim AS base

LABEL maintainer="gateway" \
      description="AI Tool Function Call Gateway" \
      version="2.0"

# Install only runtime dependencies used by the stdlib gateway and current
# built-in tools. Heavier integrations should be added through MCP or HTTP
# Actions instead of bloating the base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    jq \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ src/
COPY scripts/ scripts/
COPY config/ config/
COPY gateway.config.json gateway.config.yaml ./
COPY mcp_defaults.yaml ./

# Create directories for runtime data
RUN mkdir -p /app/data /app/workspace

# Environment variables with sensible defaults
ENV GATEWAY_PORT=8885 \
    GATEWAY_HOST=0.0.0.0 \
    GATEWAY_SQLITE_LOG_PATH=/app/data/gateway_log.sqlite3 \
    GATEWAY_CONFIG_PATH=/app/data/.gateway_service.json \
    GATEWAY_STATS_PATH=/app/data/.gateway_stats.json \
    GATEWAY_REQUEST_LOG=/app/data/.gateway_requests.jsonl \
    GATEWAY_WORKSPACE_ROOT=/app/workspace \
    PYTHONUNBUFFERED=1

EXPOSE 8885

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8885/healthz || exit 1

# Run the gateway
CMD ["python3", "src/toolcall_gateway.py", "--host", "0.0.0.0", "--port", "8885"]
