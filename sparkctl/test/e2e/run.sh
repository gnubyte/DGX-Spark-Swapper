#!/usr/bin/env bash
# End-to-end test of the installed sparkctl: a demo Spark Swapper, a small Sparkia-shaped Compose stack and the real
# Docker daemon, driven through a pseudo-terminal and checked on an emulated screen (python3-pyte).
#
#   sudo bash test/e2e/run.sh dist/sparkctl_X.Y.Z_amd64.deb [swapper-repo-dir]
#
# Needs root (it installs the package and writes /opt/sparkia), docker with compose, python3 and python3-pyte.
# It removes the test stack afterwards and leaves the package installed.
set -euo pipefail
DEB=${1:?the .deb to test}
REPO=${2:-$(cd "$(dirname "$0")/../../.." && pwd)}
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=18999
WORK=$(mktemp -d)
cleanup() {
  docker compose --project-directory /opt/sparkia -f /opt/sparkia/compose.yaml down >/dev/null 2>&1 || true
  [[ -n "${SWAPPER_PID:-}" ]] && kill "$SWAPPER_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT
[[ -e /opt/sparkia/compose.yaml && ! -e /opt/sparkia/.sparkctl-e2e ]] && { echo "/opt/sparkia holds a real Sparkia: not touching it" >&2; exit 1; }

apt-get install -y -qq "$(realpath "$DEB")" >/dev/null
sparkctl version

echo "==> a demo Spark Swapper on :$PORT (HTTPS with its self-signed certificate, as on a Spark)"
command -v openssl >/dev/null || { echo "openssl is needed for the demo Swapper's certificate" >&2; exit 1; }
SWAPPER_DEMO=1 SWAPPER_BIND=127.0.0.1 SWAPPER_PORT=$PORT \
  SWAPPER_CONF="$WORK/conf" SWAPPER_STATE="$WORK/state" python3 "$REPO/swapper/app.py" >"$WORK/swapper.log" 2>&1 &
SWAPPER_PID=$!
for _ in $(seq 60); do curl -ksf "https://127.0.0.1:$PORT/api/session" >/dev/null && break; sleep 0.5; done
curl -ksf "https://127.0.0.1:$PORT/api/session" >/dev/null || { cat "$WORK/swapper.log"; exit 1; }

echo "==> a test Sparkia stack in /opt/sparkia"
mkdir -p /opt/sparkia
touch /opt/sparkia/.sparkctl-e2e
cat > /opt/sparkia/compose.yaml <<'EOF'
name: sparkia
services:
  vision:
    image: busybox:latest
    container_name: sparkia-e2e-vision
    command: ["sh", "-c", "while true; do echo hello from e2e; sleep 2; done"]
EOF
printf 'SPARK_USER=tester\nSPARKIA_TAG=latest\nVISION_PORT=8443\n' > /opt/sparkia/.env
echo v0.9.140 > /opt/sparkia/.release
docker compose --project-directory /opt/sparkia -f /opt/sparkia/compose.yaml up -d --quiet-pull

export XDG_CONFIG_HOME="$WORK/config"
mkdir -p "$XDG_CONFIG_HOME/sparkctl"
echo "{\"swapper_url\":\"https://127.0.0.1:$PORT\",\"vision_url\":\"https://127.0.0.1:1/\"}" > "$XDG_CONFIG_HOME/sparkctl/config.json"

echo "==> driving the interface"
E2E_OUT=${E2E_OUT:-$WORK/out} python3 "$HERE/drive.py"
grep -q '^SPARKIA_TAG=0.9.142$' /opt/sparkia/.env || { echo "pinning the release didn't write .env" >&2; exit 1; }
[[ $(stat -c %a "$XDG_CONFIG_HOME/sparkctl/state.json") == 600 ]] || { echo "state.json must be 0600" >&2; exit 1; }

echo "==> the script commands"
sparkctl status
sparkctl models | grep -q serving
sparkctl doctor || true
rm -rf /opt/sparkia
echo "e2e: PASS"
