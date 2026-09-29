#!/usr/bin/env bash
# Pull the Spark Swapper docker container from GitHub Actions artifacts and
# run it.
#
#   bash pull-docker.sh [workflow-run-id]
#
# Without a run id it finds the latest successful run of the `docker`
# workflow on this repo. Requires: docker, curl, unzip, and a GitHub token
# with the `actions:read` scope for the artifact API — a fine-grained PAT,
# exported as GH_TOKEN. (Public repos sometimes allow tokenless reads.)
#
#   export GH_TOKEN=github_pat_…
#   bash pull-docker.sh
set -euo pipefail

REPO="${SWAPPER_REPO:-gnubyte/DGX-Spark-Swapper}"
ART="dgx-spark-swapper.tar"
IMG="gnubyte/dgx-spark-swapper:latest"

GH_TOKEN="${GH_TOKEN:-}"
RUN_ID="${1:-}"

api() { curl -fsSL -H "Accept: application/vnd.github+json" ${GH_TOKEN:+-H "Authorization: Bearer $GH_TOKEN"} "https://api.github.com/repos/$REPO/$1"; }

if [[ -z "$RUN_ID" ]]; then
  RUN_ID=$(api "actions/workflows/docker.yml/runs?status=success&per_page=1" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['workflow_runs'][0]['id'] if d.get('workflow_runs') else '')")
fi
[[ -n "$RUN_ID" ]] || { echo "No successful 'docker' workflow run found. Trigger one in the repo's Actions tab." >&2; exit 1; }
echo "==> using workflow run $RUN_ID"

# list the artifact id
ART_ID=$(api "actions/runs/$RUN_ID/artifacts" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(a['id'] for a in d['artifacts'] if a['name']=='dgx-spark-swapper') or '')")
[[ -n "$ART_ID" ]] || { echo "Artifact 'dgx-spark-swapper' not found on run $RUN_ID." >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL -H "Accept: application/vnd.github+json" ${GH_TOKEN:+-H "Authorization: Bearer $GH_TOKEN"} \
  "https://api.github.com/repos/$REPO/actions/artifacts/$ART_ID/zip" -o "$TMP/a.zip"
unzip -q "$TMP/a.zip" -d "$TMP"
TARBALL="$TMP/dgx-spark-swapper/$ART"

echo "==> loading image into docker"
docker load < "$TARBALL"
docker tag "$IMG" "$IMG" 2>/dev/null || true

echo "==> starting spark-swapper (host network, cert + state in /etc/spark-swapper)"
docker rm -f spark-swapper 2>/dev/null || true
docker run -d --name spark-swapper --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /etc/spark-swapper:/config \
  -v /var/lib/spark-swapper:/state \
  "$IMG"

IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1);exit}}')
echo
echo "Spark Swapper container is up:  https://${IP:-localhost}:8999"
echo "  Logs:        docker logs -f spark-swapper"
echo "  First visit: create the admin login."
