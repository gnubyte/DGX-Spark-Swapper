#!/usr/bin/env bash
# Install a downloaded Spark Swapper release over the running one, with a way back.
#
#   bash deploy/update.sh /var/lib/spark-swapper/updates/<version>/spark-swapper-<version>
#
# The Swapper's updater starts this with systemd-run, outside the service, because
# installing restarts the service. It is always the copy from the *new* release
# that runs, so a release can change how it installs itself.
#
#   1. copy /opt/spark-swapper aside
#   2. run the new release's deploy/install.sh (keeps config, logins, certificate)
#   3. wait for https://127.0.0.1:8999 to answer
#   4. if it doesn't, put the old copy back and restart the service
#
# The result goes to /var/lib/spark-swapper/update-result.json, which the
# restarted service shows on its Settings tab. Models keep serving throughout.
set -uo pipefail
NEW="${1:?usage: update.sh <unpacked release directory>}"
STATE="${SWAPPER_STATE:-/var/lib/spark-swapper}"
APP=/opt/spark-swapper
BACKUP="$STATE/app-backup"
RESULT="$STATE/update-result.json"
LOG="$STATE/update.log"
PORT="${SWAPPER_PORT:-8999}"

UNIT=/etc/systemd/system/spark-swapper.service

mkdir -p "$STATE"
exec >>"$LOG" 2>&1
echo
echo "==> $(date -Is) update.sh from $NEW"

# One update at a time, even if a second one was started by mistake.
exec 9>"$STATE/update.lock"
if ! flock -n 9; then
  echo "!! another update is running; leaving it to finish"
  exit 1
fi

old_ver=$(tr -d '[:space:]' < "$APP/VERSION" 2>/dev/null || true); old_ver=${old_ver:-unknown}
new_ver=$(tr -d '[:space:]' < "$NEW/swapper/VERSION" 2>/dev/null || true); new_ver=${new_ver:-unknown}

result() {  # state message
  python3 - "$RESULT" "$1" "$old_ver" "$new_ver" "${2:-}" <<'PY'
import json, sys, time
path, state, old, new, msg = sys.argv[1:6]
json.dump({"state": state, "from": old, "to": new, "finished": time.time(), "message": msg}, open(path, "w"))
PY
}

healthy() {
  for _ in $(seq 1 60); do
    v=$(curl -ksf --max-time 3 "https://127.0.0.1:$PORT/api/session" 2>/dev/null |
        python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null)
    [[ -n "$v" ]] && { echo "$v"; return 0; }
    sleep 2
  done
  return 1
}

if [[ ! -f "$NEW/deploy/install.sh" || ! -f "$NEW/swapper/app.py" ]]; then
  echo "!! $NEW is not a Spark Swapper release"
  result failed "The downloaded package wasn't a complete Spark Swapper release."
  exit 1
fi

echo "==> backing up $APP ($old_ver) to $BACKUP"
rm -rf "$BACKUP"
cp -a "$APP" "$BACKUP" || { result failed "Couldn't back up $APP."; exit 1; }
cp -a "$UNIT" "$BACKUP.service" 2>/dev/null || true

echo "==> installing $new_ver"
if SWAPPER_FROM_UPDATER=1 bash "$NEW/deploy/install.sh"; then
  if got=$(healthy) && [[ "$got" == "$new_ver" ]]; then
    echo "==> $new_ver is up"
    result ok ""
    exit 0
  fi
  echo "!! the service didn't come back as $new_ver (got: ${got:-nothing})"
else
  echo "!! install.sh failed"
fi

echo "==> rolling back to $old_ver"
rm -rf "$APP"
cp -a "$BACKUP" "$APP"
[[ -f "$BACKUP.service" ]] && cp -a "$BACKUP.service" "$UNIT"
systemctl daemon-reload
systemctl restart spark-swapper
if healthy >/dev/null; then
  result rolled_back "The new version didn't start; $old_ver is running again. Details: $LOG"
else
  result failed "The new version didn't start and the old one didn't come back either. Run: journalctl -u spark-swapper -n 80"
fi
exit 1
