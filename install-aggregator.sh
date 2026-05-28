#!/usr/bin/env bash
# install-aggregator.sh — установка monitor-aggregator на Ubuntu 24.04.
# Использование:
#   sudo bash install-aggregator.sh [--repo URL] [--branch BRANCH] [--prefix DIR]

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Sharkisfinetoo/ServerStatus.git}"
BRANCH="${BRANCH:-main}"
PREFIX="${PREFIX:-/opt/monitor-aggregator}"
SERVICE_USER="monitor-aggregator"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)   REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "This installer must run as root." >&2
  exit 1
fi

echo "==> Installing dependencies"
apt-get update -y
apt-get install -y --no-install-recommends python3 git ca-certificates

echo "==> Creating service user: $SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Cloning $REPO_URL ($BRANCH) into $PREFIX"
if [[ -d "$PREFIX/.git" ]]; then
  git -C "$PREFIX" fetch --depth 1 origin "$BRANCH"
  git -C "$PREFIX" reset --hard "origin/$BRANCH"
else
  rm -rf "$PREFIX"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$PREFIX"
fi

CONFIG="$PREFIX/monitor-aggregator/config/aggregator.json"
if [[ ! -f "$CONFIG.local" ]]; then
  cp "$CONFIG" "$CONFIG.local" || true
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"

echo "==> Writing systemd unit"
cat >/etc/systemd/system/monitor-aggregator.service <<EOF
[Unit]
Description=monitor-aggregator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$PREFIX/monitor-aggregator
ExecStart=/usr/bin/python3 $PREFIX/monitor-aggregator/aggregator.py
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$PREFIX/monitor-aggregator
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now monitor-aggregator.service

echo
echo "==> Done."
echo "Config:  $CONFIG  (edit, then: systemctl restart monitor-aggregator)"
echo "Logs:    journalctl -u monitor-aggregator -f"
echo "Dashboard: http://<host>:9000/"
