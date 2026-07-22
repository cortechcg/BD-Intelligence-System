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

CMD ["python", "main.py"]
