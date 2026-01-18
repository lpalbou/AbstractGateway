#!/bin/bash
# Development startup script for AbstractGateway with permissive settings

# Allow all origins (ngrok, external hosts, etc.)
export ABSTRACTGATEWAY_ALLOWED_ORIGINS="*"

# Optional: If you still have auth issues, uncomment this to disable security entirely:
# export ABSTRACTGATEWAY_SECURITY="0"

# Start the gateway on all network interfaces
cd "$(dirname "$0")"
echo "Starting AbstractGateway with permissive development settings..."
echo "- Allowed origins: *"
echo "- Host: 0.0.0.0:8080"
echo ""

abstractgateway serve --host 0.0.0.0 --port 8080
