#!/bin/sh
# Start command for the cortech-bd-dashboard WEB service (render.yaml
# `dockerCommand: dashboard/start.sh`). Lives in the repo so the shell
# quoting is checked once, here, instead of surviving YAML -> Render ->
# container shell (the 2026-09-22 exit-127 deploy). Render injects $PORT.
set -e
exec uvicorn dashboard.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-10000}" \
    --proxy-headers \
    --forwarded-allow-ips="*"
