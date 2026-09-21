# Playwright's official image ships Chromium + all required OS libraries.
# Pin to the same Playwright version as requirements.txt (1.60.0).
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

# Install Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The base image already has browsers, but re-run to guarantee the
# chromium build matching this Playwright version is present.
RUN playwright install chromium

# Copy the rest of the application.
COPY . .

# Default command = the dashboard WORKER (queue consumer, no schedule of its
# own). It must never be `python main.py`: bare main.py is the always-on
# scheduler that fires a full discovery run on boot and every 6 hours. That
# default caused the 2026-09-21 Render incident (and the earlier Railway one
# in README §"This system runs locally"). A Render/Railway service created by
# hand without a Start Command runs this CMD.
#   web service : uvicorn dashboard.app:app --host 0.0.0.0 --port $PORT
#   worker      : python -m dashboard.worker            (this default)
#   discovery   : python main.py --once                  (systemd/cron only)
CMD ["python", "-m", "dashboard.worker"]
