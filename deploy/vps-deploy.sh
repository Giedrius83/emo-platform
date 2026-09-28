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
    say "eToro keys: eToro > Settings > Trading > API Key Management (Enter skips)"
    read -r -p "  Public Key (copy button next to it): " ETORO_API_KEY < /dev/tty || true
    read -r -s -p "  API Key you created (hidden while pasting): " ETORO_USER_KEY < /dev/tty || true
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

# Private token so only your bots can post to the (public) dashboard address.
if ! grep -q '^INGEST_TOKEN=.' "$ENV_FILE" 2>/dev/null; then
  umask 077
  echo "INGEST_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(16))')" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi
INGEST_TOKEN=$(grep '^INGEST_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

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
# A system service, so the tunnel survives crashes and starts again after a
# reboot. The binary lives in /usr/local/bin: SELinux on Oracle Linux blocks
# services from executing programs in a home directory.
case "$(uname -m)" in
  aarch64|arm64) ARCH=arm64 ;;
  x86_64|amd64)  ARCH=amd64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
if [ ! -x /usr/local/bin/cloudflared ]; then
  say "Installing cloudflared ($ARCH)"
  curl -fsSL -o /tmp/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH"
  sudo install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared
  rm -f /tmp/cloudflared
  command -v restorecon >/dev/null && sudo restorecon /usr/local/bin/cloudflared || true
fi
# Retire the tunnel an earlier version of this script started by hand.
pkill -f "cloudflared tunnel --url" 2>/dev/null || true

sudo tee /etc/systemd/system/emo-tunnel.service >/dev/null <<'UNIT'
[Unit]
Description=Cloudflare quick tunnel to the trading dashboard
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate --url http://localhost:8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# Prints the tunnel's current address; the address changes whenever it restarts.
sudo tee /usr/local/bin/emo-url >/dev/null <<'HELPER'
#!/bin/sh
journalctl -u emo-tunnel --no-pager -o cat 2>/dev/null \
  | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | grep -v '://api\.' | tail -1
HELPER
sudo chmod 755 /usr/local/bin/emo-url

say "Starting the tunnel"
sudo systemctl daemon-reload
sudo systemctl enable emo-tunnel >/dev/null 2>&1
STARTED=$(date '+%Y-%m-%d %H:%M:%S')
sudo systemctl restart emo-tunnel

# 7. URL -------------------------------------------------------------------------
URL=""
for _ in $(seq 1 45); do
  # Only lines from this start: an older address in the journal is dead.
  # Skip api.trycloudflare.com: cloudflared names it in errors when a request retries.
  URL=$(sudo journalctl -u emo-tunnel --since "$STARTED" --no-pager -o cat 2>/dev/null \
        | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | grep -v '://api\.' | tail -1 || true)
  [ -n "$URL" ] && break
  sleep 2
done
[ -n "$URL" ] || { echo "No tunnel address after 90s. Log:" >&2; sudo journalctl -u emo-tunnel -n 30 --no-pager >&2; exit 1; }

# The tunnel needs a moment after printing its address before it routes traffic.
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

REPORT_URL="https://raw.githubusercontent.com/Giedrius83/emo-platform/main/deploy/report.sh"
cat <<EOF

==================================================================
 eToro:      $ETORO_STATUS

 YOUR DASHBOARD (open it in any browser, phone too):

     $URL

 If the server restarts, the address changes. Log in and run: emo-url
==================================================================

 CONNECT YOUR GROK BOTS: copy everything between the lines and send
 it to your Manager bot in Grok Bot, as one message.
------------------------------------------------------------------
Please set up dashboard reporting for the whole swarm. Do these steps on our shared computer and show me the output of each.

Step 1. Run exactly:
printf 'DASHBOARD_URL=%s\nDASHBOARD_TOKEN=%s\n' '$URL' '$INGEST_TOKEN' > ~/dashboard.env
curl -fsSL $REPORT_URL -o ~/report && chmod +x ~/report
~/report status MANAGER "dashboard reporting is live"

The last command must print a line containing "accepted":1. Anything else, show me the output.

Step 2. Send this standing rule to every bot (Scout, Planner, Quant, Guard, Trader, ORKA, Coder), follow it yourself, and ask each bot to save it permanently:

"DASHBOARD RULE: after every meaningful step, run ONE command on our shared computer, using your own name in capitals:
~/report status YOURNAME "what you are doing now"
~/report handoff YOURNAME "what you pass on" TARGETNAME   (when you hand work to another bot)
~/report done YOURNAME "a result or decision"
~/report alert YOURNAME "a problem"
Keep the text short. Never put keys, passwords or tokens in it. If the command fails, ignore it and keep working."
------------------------------------------------------------------

 If the dashboard address ever changes, send the Manager only the
 Step 1 line that starts with printf, with the new address.
==================================================================
EOF
