#!/usr/bin/env bash
# install-monitor.sh — установка server-monitor на Ubuntu 24.04.
# Использование:
#   sudo bash install-monitor.sh [--repo URL] [--branch BRANCH] [--prefix DIR]

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Sharkisfinetoo/ServerStatus.git}"
BRANCH="${BRANCH:-main}"
PREFIX="${PREFIX:-/opt/server-monitor}"
SERVICE_USER="server-monitor"

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
apt-get install -y --no-install-recommends python3 git iputils-ping ca-certificates

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

CONFIG="$PREFIX/server-monitor/config/servers.json"
if [[ ! -f "$CONFIG.local" ]]; then
  cp "$CONFIG" "$CONFIG.local" || true
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"

echo "==> Writing systemd unit"
cat >/etc/systemd/system/server-monitor.service <<EOF
[Unit]
Description=server-monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$PREFIX/server-monitor
ExecStart=/usr/bin/python3 $PREFIX/server-monitor/monitor.py
Restart=on-failure
RestartSec=5
# Ping needs CAP_NET_RAW only if not using setuid /bin/ping; on Ubuntu /bin/ping is setuid by default.
AmbientCapabilities=CAP_NET_RAW

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$PREFIX/server-monitor
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now server-monitor.service

echo
echo "==> Done."
echo "Config:  $CONFIG  (edit, then: systemctl restart server-monitor)"
echo "Logs:    journalctl -u server-monitor -f"
echo "Dashboard: http://<host>:8888/"
