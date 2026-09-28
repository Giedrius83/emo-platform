# Build context: services/voice-worker (see docker-compose.yml next to this file).
FROM node:22-slim
ENV NODE_ENV=production
WORKDIR /srv
COPY package.json package-lock.json ./
# No runtime dependencies; this only validates the lockfile and keeps the layer cacheable.
RUN npm ci --omit=dev --no-audit --no-fund
COPY src ./src
RUN useradd --system --uid 10001 voice && mkdir -p /srv/data && chown voice /srv/data
USER voice
ENV PORT=3000 DATA_DIR=/srv/data
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=10s \
  CMD node -e "fetch('http://127.0.0.1:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "--experimental-strip-types", "--no-warnings=ExperimentalWarning", "src/server.ts"]
