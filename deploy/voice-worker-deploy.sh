#!/usr/bin/env bash
# Deploy the standalone voice secretary (services/voice-worker) to a small Oracle Linux 9
# server, next to the trading dashboard, without Docker or dnf (both run out of memory on
# the free 1 GB shape). Run on the server as the opc user:
#
#   VOICE_HOST=voice.example.com bash <(curl -fsSL https://raw.githubusercontent.com/Giedrius83/emo-platform/main/deploy/voice-worker-deploy.sh)
#
# VOICE_HOST is the DNS A record pointing at this VM (Caddy gets a Let's Encrypt cert for it).
# Leave it unset to test through a Cloudflare quick tunnel before DNS exists.
# Safe to re-run: updates the code, keeps /opt/emo/voice.env, restarts the services.
# It never sets LIVE_DIAL=true. That edit is the owner's, by hand, after a greenlight.
set -euo pipefail

REPO="Giedrius83/emo-platform"
REF="${EMO_REF:-main}"
NODE_VERSION="${NODE_VERSION:-22.22.2}"
BASE=/opt/emo                       # not $HOME: SELinux stops services running programs from home directories
VOICE="$BASE/voice"
APP="$VOICE/app"
ENV_FILE="$BASE/voice.env"
DATA_DIR="$VOICE/data"
PORT=3000
VOICE_HOST="${VOICE_HOST:-}"
say() { printf '\n==> %s\n' "$*"; }

case "$(uname -m)" in
  aarch64|arm64) ARCH=arm64 ;;
  x86_64|amd64)  ARCH=x64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
CF_ARCH=$([ "$ARCH" = x64 ] && echo amd64 || echo arm64)

# 0. Clear the way (same as the dashboard script) ------------------------------------
if pgrep -f '/usr/bin/dnf' >/dev/null 2>&1; then
  say "Stopping a stuck package install from an earlier attempt"; sudo pkill -9 -f '/usr/bin/dnf' || true
fi
if systemctl list-unit-files dnf-makecache.timer >/dev/null 2>&1; then
  sudo systemctl disable --now dnf-makecache.timer >/dev/null 2>&1 || true
fi
sudo mkdir -p "$BASE" "$VOICE"
sudo chown -R "$(id -u):$(id -g)" "$BASE" 2>/dev/null || sudo chown "$(id -u):$(id -g)" "$BASE" "$VOICE"

# 1. Node 22 (one tarball, no package manager) ---------------------------------------
if [ ! -x "$BASE/node/bin/node" ] || ! "$BASE/node/bin/node" --version | grep -q "^v$NODE_VERSION\$"; then
  say "Installing Node $NODE_VERSION ($ARCH)"
  rm -rf "$BASE/node"; mkdir -p "$BASE/node"
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$ARCH.tar.gz" \
    | tar -xz -C "$BASE/node" --strip-components=1
fi
NODE="$BASE/node/bin/node"

# 2. Code --------------------------------------------------------------------------------
say "Downloading $REPO@$REF"
TMP=$(mktemp -d)
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" | tar -xz -C "$TMP"
rm -rf "$APP"; mv "$TMP"/emo-platform-* "$APP"; rm -rf "$TMP"
WORKER="$APP/services/voice-worker"
[ -f "$WORKER/src/server.ts" ] || { echo "services/voice-worker not found in $REF" >&2; exit 1; }
mkdir -p "$DATA_DIR"

# 3. Env file (secrets stay on this server; flags OFF) -----------------------------------
if [ ! -f "$ENV_FILE" ]; then
  say "Creating $ENV_FILE (flags OFF)"
  umask 077
  sed -e "s|^ENQUEUE_AUTH_SECRET=.*|ENQUEUE_AUTH_SECRET=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')|" \
      -e "s|^DATA_DIR=.*|DATA_DIR=$DATA_DIR|" \
      -e "s|^PORT=.*|PORT=$PORT|" \
      "$WORKER/.env.example" > "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
grep -q '^LIVE_DIAL=' "$ENV_FILE" || echo 'LIVE_DIAL=false' >> "$ENV_FILE"
grep -q '^OUTREACH_CALL_CHANNEL=' "$ENV_FILE" || echo 'OUTREACH_CALL_CHANNEL=false' >> "$ENV_FILE"
if ! grep -q '^TWILIO_AUTH_TOKEN=.' "$ENV_FILE" && [ -r /dev/tty ]; then
  say "Twilio credentials (console > Account Info). Enter skips; the worker then runs sandbox-only."
  read -r -p "  Account SID (AC…): " T_SID < /dev/tty || true
  read -r -s -p "  Auth Token (hidden): " T_TOKEN < /dev/tty || true; echo
  read -r -p "  From number (E.164, +47…): " T_FROM < /dev/tty || true
  if [ -n "${T_SID:-}" ] && [ -n "${T_TOKEN:-}" ] && [ -n "${T_FROM:-}" ]; then
    sed -i -e "s|^TWILIO_ACCOUNT_SID=.*|TWILIO_ACCOUNT_SID=$T_SID|" \
           -e "s|^TWILIO_AUTH_TOKEN=.*|TWILIO_AUTH_TOKEN=$T_TOKEN|" \
           -e "s|^TWILIO_FROM_NUMBER=.*|TWILIO_FROM_NUMBER=$T_FROM|" "$ENV_FILE"
    echo "  saved to $ENV_FILE"
  else
    echo "  skipped: Twilio webhooks will answer 503 until TWILIO_AUTH_TOKEN is set"
  fi
fi
if grep -q '^LIVE_DIAL=true' "$ENV_FILE"; then
  echo "NOTE: LIVE_DIAL=true is set in $ENV_FILE. This script does not change it." >&2
fi
if [ -n "$VOICE_HOST" ]; then
  sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$VOICE_HOST|" "$ENV_FILE"
fi

# 4. Worker service --------------------------------------------------------------------
say "Starting emo-voice"
if command -v restorecon >/dev/null && command -v getenforce >/dev/null && [ "$(getenforce)" != "Disabled" ]; then
  sudo restorecon -R "$BASE" || true
  sudo chcon -R -t bin_t "$BASE/node/bin" || true
fi
sudo tee /etc/systemd/system/emo-voice.service >/dev/null <<UNIT
[Unit]
Description=ArendalAI voice secretary (Twilio worker, LIVE_DIAL gated)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$WORKER
EnvironmentFile=$ENV_FILE
Environment=NODE_ENV=production
Environment=DATA_DIR=$DATA_DIR
ExecStart=$NODE --experimental-strip-types --no-warnings=ExperimentalWarning src/server.ts
Restart=always
RestartSec=5
# journald keeps and rotates the logs: journalctl -u emo-voice

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable emo-voice >/dev/null 2>&1
sudo systemctl restart emo-voice
for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null || {
  echo "emo-voice did not start. Recent logs:" >&2; sudo journalctl -u emo-voice -n 40 --no-pager >&2; exit 1; }
echo "  worker is up on 127.0.0.1:$PORT"

# 5. HTTPS: Caddy for a real hostname, Cloudflare quick tunnel otherwise -------------------
if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
  sudo firewall-cmd --permanent --add-service=http --add-service=https >/dev/null 2>&1 || true
  sudo firewall-cmd --reload >/dev/null 2>&1 || true
fi
PUBLIC_URL=""
if [ -n "$VOICE_HOST" ]; then
  if [ ! -x /usr/local/bin/caddy ]; then
    say "Installing Caddy ($CF_ARCH)"
    curl -fsSL -o /tmp/caddy "https://caddyserver.com/api/download?os=linux&arch=$CF_ARCH"
    sudo install -m 755 /tmp/caddy /usr/local/bin/caddy; rm -f /tmp/caddy
    command -v restorecon >/dev/null && sudo restorecon /usr/local/bin/caddy || true
  fi
  sudo mkdir -p /etc/caddy /var/lib/caddy
  sudo tee /etc/caddy/Caddyfile.voice >/dev/null <<CADDY
$VOICE_HOST {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
CADDY
  sudo tee /etc/systemd/system/emo-voice-caddy.service >/dev/null <<'UNIT'
[Unit]
Description=Caddy (automatic HTTPS) in front of the voice secretary
After=network-online.target emo-voice.service
Wants=network-online.target

[Service]
Environment=XDG_DATA_HOME=/var/lib
Environment=XDG_CONFIG_HOME=/var/lib
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile.voice --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile.voice --adapter caddyfile
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl disable --now emo-voice-tunnel >/dev/null 2>&1 || true
  sudo systemctl daemon-reload
  sudo systemctl enable emo-voice-caddy >/dev/null 2>&1
  sudo systemctl restart emo-voice-caddy
  PUBLIC_URL="https://$VOICE_HOST"
  say "Waiting for $PUBLIC_URL/health (certificate issuance can take a minute)"
  for _ in $(seq 1 60); do curl -fsS "$PUBLIC_URL/health" >/dev/null 2>&1 && break; sleep 3; done
  curl -fsS "$PUBLIC_URL/health" >/dev/null || echo "  not reachable yet: check DNS for $VOICE_HOST and the OCI security list (80/443). Logs: journalctl -u emo-voice-caddy" >&2
else
  if [ ! -x /usr/local/bin/cloudflared ]; then
    say "Installing cloudflared ($CF_ARCH)"
    curl -fsSL -o /tmp/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH"
    sudo install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared; rm -f /tmp/cloudflared
    command -v restorecon >/dev/null && sudo restorecon /usr/local/bin/cloudflared || true
  fi
  sudo tee /etc/systemd/system/emo-voice-tunnel.service >/dev/null <<UNIT
[Unit]
Description=Cloudflare quick tunnel to the voice secretary (testing only)
After=network-online.target emo-voice.service
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://localhost:$PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  say "Starting the quick tunnel (no VOICE_HOST given)"
  sudo systemctl daemon-reload
  sudo systemctl enable emo-voice-tunnel >/dev/null 2>&1
  STARTED=$(date '+%Y-%m-%d %H:%M:%S')
  sudo systemctl restart emo-voice-tunnel
  for _ in $(seq 1 45); do
    PUBLIC_URL=$(sudo journalctl -u emo-voice-tunnel --since "$STARTED" --no-pager -o cat 2>/dev/null \
      | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | grep -v '://api\.' | tail -1 || true)
    [ -n "$PUBLIC_URL" ] && break; sleep 2
  done
  [ -n "$PUBLIC_URL" ] || { echo "No tunnel address after 90s:" >&2; sudo journalctl -u emo-voice-tunnel -n 30 --no-pager >&2; exit 1; }
  # Twilio signatures are checked against PUBLIC_BASE_URL, so the worker must know the tunnel URL.
  sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$PUBLIC_URL|" "$ENV_FILE"
  sudo systemctl restart emo-voice
  for _ in $(seq 1 30); do curl -fsS "$PUBLIC_URL/health" >/dev/null 2>&1 && break; sleep 2; done
fi

# 6. Helper -----------------------------------------------------------------------------------
sudo tee /usr/local/bin/emo-voice >/dev/null <<HELPER
#!/usr/bin/env bash
# emo-voice status | logs [n] | env | url | smoke
set -euo pipefail
case "\${1:-status}" in
  status) curl -fsS http://127.0.0.1:$PORT/health; echo ;;
  logs)   sudo journalctl -u emo-voice -n "\${2:-60}" --no-pager -o cat ;;
  env)    echo "$ENV_FILE (edit with: sudoedit $ENV_FILE; then: sudo systemctl restart emo-voice)"; grep -E '^(OUTREACH_CALL_CHANNEL|LIVE_DIAL|PUBLIC_BASE_URL|MAX_CONCURRENT_CALLS|LLM_PROVIDER)=' "$ENV_FILE" ;;
  url)    grep '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2- ;;
  smoke)  BASE_URL=\$(grep '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2-) ENQUEUE_AUTH_SECRET=\$(grep '^ENQUEUE_AUTH_SECRET=' "$ENV_FILE" | cut -d= -f2-) bash "$WORKER/scripts/smoke.sh" ;;
  *) echo "usage: emo-voice status|logs [n]|env|url|smoke"; exit 2 ;;
esac
HELPER
sudo chmod 755 /usr/local/bin/emo-voice

HEALTH=$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null || echo '{}')
cat <<EOT

==================================================================
 Voice secretary is up.        LIVE_DIAL: $(grep -q '^LIVE_DIAL=true' "$ENV_FILE" && echo TRUE || echo false)
 Public URL:  ${PUBLIC_URL:-(none yet)}
 Health:      $HEALTH

 Twilio console (optional, for visibility):
   voice URL   ${PUBLIC_URL:-https://<host>}/v1/twilio/voice
   status URL  ${PUBLIC_URL:-https://<host>}/v1/twilio/status

 Next:  emo-voice smoke      # sandbox smoke, zero dials
        emo-voice env        # flags; secrets stay in $ENV_FILE
 Live dialing stays OFF until you edit LIVE_DIAL yourself after a greenlight.
==================================================================
EOT
