#!/usr/bin/env bash
# Installs or updates the TRMNL UniFi dashboard as a systemd service.
# Run as root on any always-on Debian/Ubuntu machine or LXC on the same LAN as the console:
#   curl -fsSL https://raw.githubusercontent.com/nguyenware/trmnl-unifi/main/install.sh | bash
set -euo pipefail

REPO="${REPO:-https://github.com/nguyenware/trmnl-unifi.git}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/trmnl-unifi}"
SVC_USER=trmnl-unifi

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (inside the LXC: pct enter <id>, or sudo bash install.sh)." >&2
  exit 1
fi

echo "==> Installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl ca-certificates >/dev/null

echo "==> Fetching code into $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi

echo "==> Setting up Python environment"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "==> Creating service user '$SVC_USER'"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"

# The service user must be able to read the code and venv, whatever root's umask is.
chmod -R go+rX "$DIR"

if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  echo "==> Created $DIR/.env from the example"
fi
# .env holds the API keys: readable by root and the service only.
chown root:"$SVC_USER" "$DIR/.env"
chmod 640 "$DIR/.env"

echo "==> Installing systemd unit"
cp "$DIR/trmnl-unifi.service" /etc/systemd/system/trmnl-unifi.service
systemctl daemon-reload
systemctl enable -q trmnl-unifi

if grep -qE '^(UNIFI_API_KEY=$|TRMNL_WEBHOOK_URL=https://trmnl.com/api/custom_plugins/your-plugin-uuid)' "$DIR/.env"; then
  echo
  echo "Next: fill in UNIFI_HOST, UNIFI_API_KEY and TRMNL_WEBHOOK_URL in $DIR/.env (nano $DIR/.env), then:"
  echo "  runuser -u $SVC_USER -- $DIR/.venv/bin/python $DIR/unifi_dashboard.py check"
  echo "  systemctl restart trmnl-unifi && journalctl -u trmnl-unifi -f"
  exit 0
fi

echo "==> Checking the UniFi APIs"
if runuser -u "$SVC_USER" -- "$DIR/.venv/bin/python" "$DIR/unifi_dashboard.py" check; then
  systemctl restart trmnl-unifi
  echo
  echo "Running. Follow the log with: journalctl -u trmnl-unifi -f"
else
  echo "    Fix the settings in $DIR/.env, re-run the check, then: systemctl restart trmnl-unifi"
fi
