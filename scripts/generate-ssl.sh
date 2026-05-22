#!/bin/bash
# Generate self-signed SSL certificates for development/testing
# For production, use Let's Encrypt or proper CA certificates

set -e

SSL_DIR="$(dirname "$0")/../nginx/ssl"
mkdir -p "$SSL_DIR"

DOMAIN="${1:-localhost}"

echo "Generating self-signed SSL certificate for: $DOMAIN"

openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$SSL_DIR/key.pem" \
    -out "$SSL_DIR/cert.pem" \
    -subj "/C=US/ST=State/L=City/O=Organization/CN=$DOMAIN"

echo "SSL certificate generated:"
echo "  Certificate: $SSL_DIR/cert.pem"
echo "  Private Key: $SSL_DIR/key.pem"
echo ""
echo "To use with nginx, uncomment the SSL lines in nginx/nginx.conf"
