#!/usr/bin/env bash
# Deploy the trading dashboard to a small Linux server (built for Oracle Linux 9
# on Oracle's free 1 GB shape) and expose it through a Cloudflare quick tunnel.
# Run on the server as the opc user:
#
#   curl -fsSL https://raw.githubusercontent.com/Giedrius83/emo-platform/main/deploy/vps-deploy.sh | bash
#
# Safe to re-run: it updates the code, keeps your keys, and restarts services.
#
# It deliberately avoids dnf and Docker: on a 1 GB server dnf runs out of memory
# processing package metadata. Everything here is a single downloaded binary or
# a prebuilt file, so the whole run needs well under 200 MB of memory.
set -euo pipefail

REPO="Giedrius83/emo-platform"
BASE=/opt/emo          # not $HOME: SELinux stops services running programs from home directories
APP="$BASE/app"
ENV_FILE="$BASE/.env"
TRADING_ENV_FILE="$BASE/trading.env"   # trading keys: read by the trader service only
TRADING_DB="$BASE/data/trading.db"
say() { printf '\n==> %s\n' "$*"; }

case "$(uname -m)" in
  aarch64|arm64) ARCH=arm64; UV_ARCH=aarch64 ;;
  x86_64|amd64)  ARCH=amd64; UV_ARCH=x86_64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

# 0. Clear the way -------------------------------------------------------------
# An earlier version of this script ran dnf, which can sit stuck for hours on a
# small server. It is no longer needed; stop it so it frees memory.
if pgrep -f '/usr/bin/dnf' >/dev/null 2>&1; then
  say "Stopping a stuck package install from an earlier attempt"
  sudo pkill -9 -f '/usr/bin/dnf' || true
fi

# Oracle Linux refreshes its package lists in the background (dnf-makecache).
# On a 500 MB server that alone can freeze the machine, taking the dashboard
# and SSH down with it. Nothing here needs it, so turn it off.
if systemctl list-unit-files dnf-makecache.timer >/dev/null 2>&1; then
  sudo systemctl disable --now dnf-makecache.timer >/dev/null 2>&1 || true
fi

MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
SWAP_KB=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
if [ "$MEM_KB" -lt 3000000 ] && [ "$SWAP_KB" -lt 1000000 ] && [ ! -e /swapfile ]; then
  say "Small server ($((MEM_KB / 1024)) MB memory): adding 2 GB of swap"
  sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo mkdir -p "$BASE"
sudo chown "$(id -u):$(id -g)" "$BASE"

# 1. Code ------------------------------------------------------------------------
# A tarball, so git is not needed. The dashboard comes prebuilt in deploy/web.
say "Downloading the latest code"
TMP=$(mktemp -d)
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" | tar -xz -C "$TMP"
rm -rf "$APP"
mv "$TMP"/emo-platform-main "$APP"
rm -rf "$TMP"

# 2. Python ----------------------------------------------------------------------
# uv is one static binary; it fetches a standalone Python 3.11 into /opt/emo, so
# the system's Python 3.9 and package manager are never touched.
if [ ! -x "$BASE/bin/uv" ]; then
  say "Installing uv (Python manager)"
  mkdir -p "$BASE/bin"
  curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$UV_ARCH-unknown-linux-gnu.tar.gz" \
    | tar -xz -C "$BASE/bin" --strip-components=1
fi
export UV_PYTHON_INSTALL_DIR="$BASE/python" UV_CACHE_DIR="$BASE/cache"
say "Preparing Python 3.11 and the service's libraries (1-3 minutes)"
[ -x "$BASE/venv/bin/python" ] || "$BASE/bin/uv" venv --quiet --python 3.11 --python-preference only-managed "$BASE/venv"
"$BASE/bin/uv" pip install --quiet --python "$BASE/venv/bin/python" -r "$APP/services/telemetry/requirements.txt"

# 3. eToro keys -------------------------------------------------------------------
# Stored in /opt/emo/.env, readable only by you. Asked once; delete the file and
# re-run to change them. Skip them and the account numbers are simulated.
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
      } >> "$ENV_FILE"
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
  echo "INGEST_TOKEN=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
INGEST_TOKEN=$(grep '^INGEST_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

# 4. Service ---------------------------------------------------------------------
say "Starting the dashboard service"
# SELinux: reset /opt/emo to its default labels, then mark the program folders
# as executables so the service manager is allowed to start them.
if command -v restorecon >/dev/null && command -v getenforce >/dev/null && [ "$(getenforce)" != "Disabled" ]; then
  sudo restorecon -R "$BASE" || true
  for dir in "$BASE"/bin "$BASE"/venv/bin "$BASE"/python/*/bin; do
    [ -d "$dir" ] && sudo chcon -R -t bin_t "$dir" || true
  done
fi
sudo tee /etc/systemd/system/emo-dashboard.service >/dev/null <<UNIT
[Unit]
Description=Trading dashboard (telemetry API and web page)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$APP/services/telemetry
EnvironmentFile=$ENV_FILE
Environment=STATIC_DIR=$APP/deploy/web
Environment=TRADING_DB_PATH=$TRADING_DB
ExecStart=$BASE/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips *
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable emo-dashboard >/dev/null 2>&1
sudo systemctl restart emo-dashboard

# 4b. Trader (off until you turn it on) -------------------------------------------
# A separate service with its own key file, so the dashboard process never holds
# trading keys. It stays stopped until `emo-trading setup` enables DEMO trading.
mkdir -p "$BASE/data"
[ -f "$TRADING_ENV_FILE" ] || { umask 077; : > "$TRADING_ENV_FILE"; }
chmod 600 "$TRADING_ENV_FILE"
sudo tee /etc/systemd/system/emo-trader.service >/dev/null <<UNIT
[Unit]
Description=eToro crypto trader (SCOUT -> QUANT -> GUARD -> TRADER)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$APP/services/telemetry
EnvironmentFile=$TRADING_ENV_FILE
Environment=TRADING_DB_PATH=$TRADING_DB
ExecStart=$BASE/venv/bin/python -m app.trading run
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT
sudo tee /usr/local/bin/emo-trading >/dev/null <<HELPER
#!/usr/bin/env bash
# emo-trading setup | status | logs | stop | start | recover "what you checked"
set -euo pipefail
ENVF="$TRADING_ENV_FILE"
run() { cd "$APP/services/telemetry" && set -a && . "\$ENVF" && set +a && TRADING_DB_PATH="$TRADING_DB" "$BASE/venv/bin/python" -m app.trading "\$@"; }
case "\${1:-status}" in
  setup)
    echo "DEMO trading setup. Create the key in eToro: Settings > Trading > API Key Management,"
    echo "environment DEMO, permission WRITE. Nothing typed here is shown or logged."
    read -r -p "  Public Key: " K1
    read -r -s -p "  DEMO API Key (hidden): " K2; echo
    [ -n "\$K1" ] && [ -n "\$K2" ] || { echo "both keys are needed"; exit 1; }
    umask 077
    grep -v -E '^(ETORO_TRADING_API_KEY|ETORO_TRADING_USER_KEY|TRADING_ENV|TRADING_PIPELINE_ENABLED)=' "\$ENVF" > "\$ENVF.tmp" || true
    printf 'ETORO_TRADING_API_KEY=%s\nETORO_TRADING_USER_KEY=%s\nTRADING_ENV=demo\nTRADING_PIPELINE_ENABLED=true\n' "\$K1" "\$K2" >> "\$ENVF.tmp"
    mv "\$ENVF.tmp" "\$ENVF"; chmod 600 "\$ENVF"
    sudo systemctl enable --now emo-trader >/dev/null 2>&1; sudo systemctl restart emo-trader
    sleep 8; sudo journalctl -u emo-trader -n 15 --no-pager -o cat ;;
  status)  run status ;;
  logs)    sudo journalctl -u emo-trader -n "\${2:-60}" --no-pager -o cat ;;
  stop)    sudo systemctl disable --now emo-trader && echo "trader stopped; open positions keep their eToro TP and backstop SL" ;;
  start)   sudo systemctl enable --now emo-trader && echo started ;;
  recover) shift; run recover --reason "\$*" ;;
  *) echo "usage: emo-trading setup|status|logs|stop|start|recover <reason>"; exit 2 ;;
esac
HELPER
sudo chmod 755 /usr/local/bin/emo-trading
sudo systemctl daemon-reload
if grep -q '^TRADING_PIPELINE_ENABLED=true' "$TRADING_ENV_FILE" 2>/dev/null; then
  sudo systemctl enable emo-trader >/dev/null 2>&1
  sudo systemctl restart emo-trader
  TRADER_STATUS="running (see: emo-trading status)"
else
  TRADER_STATUS="off. To trade on DEMO run: emo-trading setup"
fi

# 5. Health ----------------------------------------------------------------------
say "Waiting for http://localhost:8000/health"
for _ in $(seq 1 45); do
  curl -fsS http://localhost:8000/health >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://localhost:8000/health >/dev/null || {
  echo "The service did not start. Recent logs:" >&2
  sudo journalctl -u emo-dashboard -n 40 --no-pager >&2
  exit 1
}
echo "  service is up"

# 6. Tunnel ----------------------------------------------------------------------
# A system service, so the tunnel survives crashes and starts again after a
# reboot. The binary lives in /usr/local/bin: SELinux on Oracle Linux blocks
# services from executing programs in a home directory.
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
After=network-online.target emo-dashboard.service
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
 Trader:     $TRADER_STATUS

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
