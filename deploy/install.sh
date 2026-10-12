#!/usr/bin/env bash
# Install / update Spark Swapper on a DGX Spark or an AMD Strix Halo box.   sudo bash install.sh
#
# Copies the app to /opt/spark-swapper, renders the public models.json site
# template into /etc/spark-swapper/models.json with real values (user, host IP,
# openclaw path), generates a self-signed cert, and runs the systemd service.
# An existing /etc/spark-swapper/models.json is preserved (override wins); the
# model catalog (catalog.json) ships with the app, so updates bring new models.
#
# SWAPPER_AUTO_UPDATE=0 turns off automatic updates (the weekly check still shows
# when a new version is out); =1 turns them back on. Unset leaves the setting alone.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo:  sudo bash $0" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/spark-swapper
CONF=/etc/spark-swapper
STATE=/var/lib/spark-swapper
PORT=8999

# ── figure out real values for the template ─────────────────────────────
# The web user: env override, else the first non-root account with a home.
USER_="${SWAPPER_USER:-}"
if [[ -z "$USER_" ]]; then
  for h in /home/*; do
    [[ -d "$h" ]] && USER_="$(basename "$h")" && break
  done
  USER_="${USER_:-root}"
fi
# Public host IP: the default-route source, else first LAN IP.
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1);exit}}')
[[ -n "${IP:-}" ]] || IP=$(hostname -I 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i!="127.0.0.1"){print $i;exit}}')
[[ -n "${IP:-}" ]] || IP=$(hostname)
# OpenClaw binary: env override, else whichever of the usual paths exists.
OC_BIN="${OPENCLAW_BIN:-}"
for p in "/home/$USER_/.npm-global/bin/openclaw" "/usr/local/bin/openclaw" "/usr/bin/openclaw"; do
  [[ -x "$p" ]] && OC_BIN="$p" && break
done
OC_BIN="${OC_BIN:-/home/$USER_/.npm-global/bin/openclaw}"

python3 - <<'PY' || { echo "Spark Swapper needs Python 3.10 or newer." >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

echo "==> installing app $(cat "$SRC/swapper/VERSION" 2>/dev/null || echo '?') to $APP"
install -d -m 0755 "$APP" "$APP/web"
install -m 0644 "$SRC"/swapper/*.py "$APP/"
install -m 0644 "$SRC"/swapper/models.json "$SRC"/swapper/catalog.json "$SRC"/swapper/VERSION "$APP/"
install -m 0644 "$SRC"/swapper/web/* "$APP/web/"
install -d -m 0700 "$CONF" "$STATE"

# ── automatic updates (opt out with SWAPPER_AUTO_UPDATE=0) ──
if [[ -n "${SWAPPER_AUTO_UPDATE:-}" ]]; then
  python3 - "$STATE/state.json" "$SWAPPER_AUTO_UPDATE" <<'PY'
import json, sys
path, on = sys.argv[1], sys.argv[2] not in ("0", "false", "no", "off")
try:
    st = json.load(open(path))
except (OSError, ValueError):
    st = {}
st.setdefault("updates", {})["auto_install"] = on
json.dump(st, open(path, "w"), indent=2)
PY
  echo "==> automatic updates: $([[ "$SWAPPER_AUTO_UPDATE" =~ ^(0|false|no|off)$ ]] && echo off || echo on)"
fi

# ── render the template (only if the box doesn't already have a config) ──
if [[ ! -s "$CONF/models.json" ]]; then
  echo "==> rendering config: user=$USER_ host=$IP openclaw=$OC_BIN"
  # A fresh box gets its own API key instead of the well-known "vllm-local".
  KEY="sk-spark-$(openssl rand -hex 20)"
  sed -e "s/{{USER}}/$USER_/g" \
      -e "s/{{PUBLIC_HOST}}/$IP/g" \
      -e "s/{{OPENCLAW_BIN}}/$OC_BIN/g" \
      -e "s/\"api_key\": \"vllm-local\"/\"api_key\": \"$KEY\"/" \
      "$APP/models.json" > "$CONF/models.json"
  chmod 640 "$CONF/models.json"
else
  echo "==> keeping existing $CONF/models.json (override wins)"
fi

# ── self-signed cert (10 years) ──
if [[ ! -s "$CONF/tls.crt" ]]; then
  echo "==> generating a self-signed certificate (10 years)"
  SAN="DNS:localhost,DNS:$(hostname),IP:127.0.0.1"
  for ip in $(hostname -I); do [[ "$ip" == *:* ]] || SAN+=",IP:$ip"; done
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CONF/tls.key" -out "$CONF/tls.crt" \
    -subj "/CN=Spark Swapper ($(hostname))" -addext "subjectAltName=$SAN" 2>/dev/null
  chmod 600 "$CONF/tls.key"
fi

# ── HTTPS front helper (also used by DSH's Spark guide) ──
cat > /usr/local/sbin/spark-swapper-front <<'FRONT'
#!/usr/bin/env bash
# Set up nginx on :11443 for the model API, requiring the API key (Provisioning ▸ HTTPS front).
exec /usr/bin/python3 /opt/spark-swapper/app.py --setup-front "$@"
FRONT
chmod 755 /usr/local/sbin/spark-swapper-front

# ── reset-login helper ──
cat > /usr/local/sbin/spark-swapper-reset-login <<'RESET'
#!/usr/bin/env bash
# Forget all web logins so the next visitor to https://<spark>:8999 creates a new admin.
set -e
rm -f /etc/spark-swapper/users.json /etc/spark-swapper/secret.key /etc/spark-swapper/tokens.json
systemctl restart spark-swapper
echo "Logins cleared. Visit the page to create the admin login."
RESET
chmod 755 /usr/local/sbin/spark-swapper-reset-login

# ── systemd unit ──
install -m 0644 "$SRC/deploy/spark-swapper.service" /etc/systemd/system/spark-swapper.service
systemctl daemon-reload
systemctl enable spark-swapper >/dev/null 2>&1
systemctl restart spark-swapper

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null && echo "==> ufw: allowed $PORT/tcp"
fi

# An HTTPS front written before 2.0 (by DSH's Spark guide) doesn't check the API key, which
# 2.0's TensorFold and Nemotron models need: replace it with Spark Swapper's own.
FRONT_CONF=/etc/nginx/conf.d/spark-model-front.conf
if [[ -f "$FRONT_CONF" ]] && grep -q "Written by DSH for Windows" "$FRONT_CONF"; then
  echo "==> upgrading the :11443 HTTPS front so it requires the API key"
  /usr/local/sbin/spark-swapper-front || echo "   (couldn't — run Provisioning ▸ HTTPS front on the page)"
fi

for _ in $(seq 1 120); do
  curl -ksf "https://127.0.0.1:$PORT/api/session" >/dev/null && break; sleep 0.5
done
if curl -ksf "https://127.0.0.1:$PORT/api/session" >/dev/null; then
  echo
  echo "  Spark Swapper is up:  https://${IP}:$PORT"
  echo "  (self-signed certificate: accept the browser warning once,"
  echo "   or trust the cert — see the Provisioning tab in the UI)"
  echo "  First visit: create the admin login.  Forgot it?  sudo spark-swapper-reset-login"
  echo "  Updates install themselves on Sundays (turn that off on the Settings tab)."
else
  echo "Service did not answer; see: journalctl -u spark-swapper -n 50" >&2
  exit 1
fi
