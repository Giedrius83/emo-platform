# Build context is the repository root (see docker-compose.yml).

# 1. Build the dashboard. Debian-based node, so the native build tools match
#    the lockfile's linux-*-gnu packages on both x86 and ARM servers.
FROM node:22-slim AS web
WORKDIR /web
COPY apps/trading-terminal/package.json apps/trading-terminal/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/trading-terminal/ ./
# No VITE_TELEMETRY_URL: the dashboard talks to the server that serves it.
RUN npm run build

# 2. The service, which also serves the built dashboard.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATIC_DIR=/srv/web
WORKDIR /srv
COPY services/telemetry/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/telemetry/app ./app
COPY --from=web /web/dist ./web

# Run unprivileged; the service needs no root.
RUN useradd --system --uid 10001 telemetry
USER telemetry

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
