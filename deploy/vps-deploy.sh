#!/usr/bin/env bash
# Deploy the telemetry backend to an Oracle Linux 9 VPS and expose it through a
# Cloudflare quick tunnel. Run on the VPS as the opc user:
#
#   curl -fsSL https://raw.githubusercontent.com/Giedrius83/emo-platform/main/deploy/vps-deploy.sh | bash
#
# Safe to re-run: it pulls the latest code, rebuilds, and restarts the tunnel.
set -euo pipefail

REPO_URL="https://github.com/Giedrius83/emo-platform.git"
APP_DIR="$HOME/emo-platform"
TUNNEL_DIR="$HOME/cloudflared"
say() { printf '\n==> %s\n' "$*"; }

# 1. Docker CE -----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker CE"
  sudo dnf install -y dnf-utils
  sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
  sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
else
  say "Docker already installed: $(docker --version)"
  sudo systemctl enable --now docker
fi
# Group membership only applies to new logins, so use sudo for this run.
DOCKER="sudo docker"

# 2. Code ------------------------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
  say "Updating $APP_DIR"
  git -C "$APP_DIR" pull --ff-only
else
  say "Cloning $REPO_URL"
  sudo dnf install -y git >/dev/null
  git clone "$REPO_URL" "$APP_DIR"
fi

# 3. eToro keys -----------------------------------------------------------------
# Stored in $APP_DIR/.env (git-ignored, readable only by you). Asked once; delete
# the file and re-run to change them. Skip them and the account is simulated.
ENV_FILE="$APP_DIR/.env"
if ! grep -q '^ETORO_USER_KEY=.' "$ENV_FILE" 2>/dev/null; then
  if [ -r /dev/tty ]; then
    say "eToro API keys (read-only access is enough; press Enter to skip)"
    read -r -p "  x-api-key  (application key): " ETORO_API_KEY < /dev/tty || true
    read -r -s -p "  x-user-key (user key, hidden): " ETORO_USER_KEY < /dev/tty || true
    echo
    read -r -p "  account [real/demo, default real]: " ETORO_ACCOUNT < /dev/tty || true
    if [ -n "${ETORO_API_KEY:-}" ] && [ -n "${ETORO_USER_KEY:-}" ]; then
      umask 077
      {
        echo "ETORO_API_KEY=$ETORO_API_KEY"
        echo "ETORO_USER_KEY=$ETORO_USER_KEY"
        echo "ETORO_ACCOUNT=${ETORO_ACCOUNT:-real}"
      } > "$ENV_FILE"
      chmod 600 "$ENV_FILE"
      echo "  saved to $ENV_FILE"
    else
      echo "  skipped: the dashboard will show SIMULATED account numbers"
    fi
  else
    echo "No terminal to ask for eToro keys; running with simulated account numbers." >&2
  fi
fi

# 4. Build and start ------------------------------------------------------------
say "Building and starting the telemetry service"
cd "$APP_DIR"
$DOCKER compose up -d --build

# 5. Health ----------------------------------------------------------------------
say "Waiting for http://localhost:8000/health"
for _ in $(seq 1 45); do
  if curl -fsS http://localhost:8000/health; then echo; break; fi
  sleep 2
done
curl -fsS http://localhost:8000/health >/dev/null || {
  echo "Service did not become healthy. Recent logs:" >&2
  $DOCKER compose logs --tail 50 telemetry >&2
  exit 1
}

# 6. Tunnel ----------------------------------------------------------------------
case "$(uname -m)" in
  aarch64|arm64) ARCH=arm64 ;;
  x86_64|amd64)  ARCH=amd64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
mkdir -p "$TUNNEL_DIR"
cd "$TUNNEL_DIR"
if [ ! -x ./cloudflared ]; then
  say "Downloading cloudflared ($ARCH)"
  curl -fsSL -o cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH"
  chmod +x cloudflared
fi

say "Starting the quick tunnel"
pkill -f "cloudflared tunnel --url" 2>/dev/null || true
: > cloudflared.log
nohup ./cloudflared tunnel --no-autoupdate --url http://localhost:8000 > cloudflared.log 2>&1 &

# 7. URL -------------------------------------------------------------------------
URL=""
for _ in $(seq 1 45); do
  # Skip api.trycloudflare.com: cloudflared names it in errors when a request retries.
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' cloudflared.log | grep -v '://api\.' | head -1 || true)
  [ -n "$URL" ] && break
  sleep 2
done
[ -n "$URL" ] || { echo "No tunnel URL after 90s. Log:" >&2; tail -30 cloudflared.log >&2; exit 1; }

# The tunnel needs a moment after printing its URL before it routes traffic.
for _ in $(seq 1 30); do
  curl -fsS "$URL/health" >/dev/null 2>&1 && break
  sleep 2
done

# Plain %-formatting: Oracle Linux 9 ships Python 3.9, which rejects nested f-string quotes.
STATUS_PY=$(cat <<'PY'
import json, sys
e = json.load(sys.stdin).get("etoro")
if not e:
    print("not configured (account numbers are SIMULATED)")
elif e.get("connected"):
    print("connected to %s account: $%s, %s open, %s closed trades"
          % (e["account"], e["equity"], e["open_positions"], e["closed_trades"]))
else:
    print("NOT connected: %s" % (e.get("last_error") or "waiting for first poll"))
PY
)
sleep 5  # give the first eToro poll a moment
ETORO_STATUS=$(curl -fsS http://localhost:8000/health | python3 -c "$STATUS_PY" 2>/dev/null || echo "unknown")

cat <<EOF

==================================================================
 eToro:        $ETORO_STATUS
 Tunnel URL:   $URL
 Health:       $(curl -fsS "$URL/health" 2>/dev/null || echo "not routing yet, retry in a minute")
 Socket:       ${URL/https:/wss:}/ws/telemetry

 Set this in Vercel as VITE_TELEMETRY_URL (https, not wss):
     $URL
==================================================================
EOF
