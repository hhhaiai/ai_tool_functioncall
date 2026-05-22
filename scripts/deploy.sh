#!/usr/bin/env bash
# Gateway deployment script

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "docker compose or docker-compose is required" >&2
    exit 1
fi

# Parse arguments
ENVIRONMENT="${1:-development}"
ACTION="${2:-up}"

case "$ENVIRONMENT" in
    development|dev)
        COMPOSE_FILE="docker-compose.yml"
        ;;
    production|prod)
        COMPOSE_FILE="docker-compose.prod.yml"
        ;;
    *)
        echo "Usage: $0 {development|production} {up|down|restart|logs|status}"
        exit 1
        ;;
esac

case "$ACTION" in
    up)
        echo "Starting gateway in $ENVIRONMENT mode..."
        "${COMPOSE[@]}" -f "$COMPOSE_FILE" up -d
        echo "Gateway started. Check status with: $0 $ENVIRONMENT status"
        ;;
    down)
        echo "Stopping gateway..."
        "${COMPOSE[@]}" -f "$COMPOSE_FILE" down
        ;;
    restart)
        echo "Restarting gateway..."
        "${COMPOSE[@]}" -f "$COMPOSE_FILE" restart
        ;;
    logs)
        "${COMPOSE[@]}" -f "$COMPOSE_FILE" logs -f
        ;;
    status)
        echo "Gateway status:"
        "${COMPOSE[@]}" -f "$COMPOSE_FILE" ps
        echo ""
        echo "Health check:"
        curl -s http://localhost:${GATEWAY_PORT:-8885}/healthz | python3 -m json.tool 2>/dev/null || echo "Gateway not responding"
        ;;
    build)
        echo "Building gateway image..."
        "${COMPOSE[@]}" -f "$COMPOSE_FILE" build
        ;;
    *)
        echo "Usage: $0 {development|production} {up|down|restart|logs|status|build}"
        exit 1
        ;;
esac
