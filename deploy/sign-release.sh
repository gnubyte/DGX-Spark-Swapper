#!/usr/bin/env bash
# Sign a published Spark Swapper release with a key that never touches GitHub.
#
#   deploy/sign-release.sh 2.0.1 ~/.ssh/spark-swapper-release
#
# Downloads spark-swapper-<version>.tar.gz from the release, checks it against the
# release's SHA256SUMS, signs it with `ssh-keygen -Y sign` and uploads the .sig.
# Boxes whose installed copy has swapper/release-signers refuse releases without
# a valid signature from a key listed there.
#
# One-time setup:
#   ssh-keygen -t ed25519 -f ~/.ssh/spark-swapper-release -C "Spark Swapper releases"
#   printf 'spark-swapper-release namespaces="spark-swapper-release" %s\n' \
#     "$(cut -d' ' -f1,2 ~/.ssh/spark-swapper-release.pub)" > swapper/release-signers
#   git add swapper/release-signers && git commit -m "Sign releases"   # ships with the next release
# Until then, sign each release here *before* the following Sunday.
set -euo pipefail
VER="${1:?usage: sign-release.sh <version> <private key>}"
KEY="${2:?usage: sign-release.sh <version> <private key>}"
REPO="${SWAPPER_REPO:-gnubyte/DGX-Spark-Swapper}"
NAME="spark-swapper-$VER.tar.gz"
DIR="$(mktemp -d)"
trap 'rm -rf "$DIR"' EXIT
cd "$DIR"
gh release download "v$VER" -R "$REPO" -p "$NAME" -p SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS
ssh-keygen -Y sign -f "$KEY" -n spark-swapper-release "$NAME"
if [[ -f "$OLDPWD/swapper/release-signers" ]]; then
  ssh-keygen -Y verify -f "$OLDPWD/swapper/release-signers" -I spark-swapper-release \
    -n spark-swapper-release -s "$NAME.sig" < "$NAME"
fi
gh release upload "v$VER" -R "$REPO" "$NAME.sig" --clobber
echo "Signed and uploaded $NAME.sig"
