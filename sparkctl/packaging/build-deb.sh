#!/usr/bin/env bash
# Build sparkctl .deb packages: bash packaging/build-deb.sh [ARCH ...]   (default: arm64 amd64)
# Needs Go and dpkg-deb (any Debian or Ubuntu with golang, or run inside golang:1.24). Packages land in dist/.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${VERSION:-$(tr -d ' \n' < VERSION)}
ARCHES=("$@")
(( ${#ARCHES[@]} )) || ARCHES=(arm64 amd64)
OUT=${OUT:-dist}
mkdir -p "$OUT"

command -v dpkg-deb >/dev/null || { echo "dpkg-deb is needed (apt install dpkg)" >&2; exit 1; }

# the packaged config is the built-in defaults, written by the program itself
go run . default-config > "$OUT/config.json"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$OUT/config.json" 2>/dev/null || grep -q '"swapper_url"' "$OUT/config.json"

for arch in "${ARCHES[@]}"; do
  case $arch in
    arm64|amd64|armhf|riscv64|ppc64le|s390x) ;;
    *) echo "unknown architecture $arch" >&2; exit 1 ;;
  esac
  goarch=$arch
  [[ $arch == armhf ]] && goarch=arm
  root=$(mktemp -d)
  trap 'rm -rf "$root"' EXIT
  echo "==> sparkctl $VERSION for $arch"
  CGO_ENABLED=0 GOOS=linux GOARCH=$goarch GOARM=7 go build -trimpath \
    -ldflags "-s -w -X main.version=$VERSION -buildid=" -o "$root/usr/bin/sparkctl" .
  install -d "$root/DEBIAN" "$root/etc/sparkctl" "$root/usr/share/doc/sparkctl" "$root/usr/share/man/man1" \
    "$root/usr/share/bash-completion/completions" "$root/usr/share/lintian/overrides"
  # Go builds one static binary on purpose: it runs on any Ubuntu or Debian release without a matching libc
  echo "sparkctl: statically-linked-binary [usr/bin/sparkctl]" > "$root/usr/share/lintian/overrides/sparkctl"
  chmod 0644 "$root/usr/share/lintian/overrides/sparkctl"
  install -m 0644 "$OUT/config.json" "$root/etc/sparkctl/config.json"
  install -m 0644 README.md "$root/usr/share/doc/sparkctl/README.md"
  install -m 0644 packaging/copyright "$root/usr/share/doc/sparkctl/copyright"
  gzip -9n < packaging/sparkctl.1 > "$root/usr/share/man/man1/sparkctl.1.gz"
  {
    echo "sparkctl ($VERSION) stable; urgency=medium"
    echo
    echo "  * Release $VERSION (see the sparkctl folder of gnubyte/DGX-Spark-Swapper)."
    echo
    echo " -- Spark Swapper maintainers <noreply@users.noreply.github.com>  $(date -R -u -d @"${SOURCE_DATE_EPOCH:-$(date +%s)}")"
  } | gzip -9n > "$root/usr/share/doc/sparkctl/changelog.gz"
  go run . completion > "$root/usr/share/bash-completion/completions/sparkctl"
  chmod 0644 "$root/usr/share/bash-completion/completions/sparkctl"
  chmod 0755 "$root/usr/bin/sparkctl"
  size=$(du -sk --exclude=DEBIAN "$root" | cut -f1)
  cat > "$root/DEBIAN/control" <<EOF
Package: sparkctl
Version: $VERSION
Architecture: $arch
Maintainer: Spark Swapper maintainers <noreply@users.noreply.github.com>
Installed-Size: $size
Section: admin
Priority: optional
Recommends: sudo
Suggests: docker.io | docker-ce, docker-compose-v2 | docker-compose-plugin
Homepage: https://github.com/gnubyte/DGX-Spark-Swapper
Description: terminal manager for Spark Swapper, Sparkia and Docker on a DGX Spark
 sparkctl is one full-screen terminal app for a DGX Spark running Spark
 Swapper and Sparkia: swap and install models and change their options,
 manage the Sparkia Docker Compose stack, its releases and its firewall,
 work with Docker containers, images and volumes, control the systemd
 services, and follow, search and save every log. Actions show the command
 they run, ask before what can't be undone, and get root through sudo.
 .
 It also has commands for scripts: sparkctl status, swap, models, logs and
 doctor.
EOF
  echo "/etc/sparkctl/config.json" > "$root/DEBIAN/conffiles"
  find "$root" -type d -exec chmod 0755 {} +
  deb="$OUT/sparkctl_${VERSION}_${arch}.deb"
  dpkg-deb --root-owner-group -Zxz --build "$root" "$deb" >/dev/null
  rm -rf "$root"
  trap - EXIT
  echo "    $deb ($(du -h "$deb" | cut -f1))"
done
( cd "$OUT" && sha256sum sparkctl_"${VERSION}"_*.deb > SHA256SUMS )
rm -f "$OUT/config.json"
