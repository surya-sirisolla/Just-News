#!/bin/sh
# One-time setup of a fresh Ubuntu 24.04 server (run as root):
#   curl -fsSL https://raw.githubusercontent.com/surya-sirisolla/Just-News/main/deploy/setup-server.sh | sudo sh
# Then copy .env (and optionally data/) into /opt/justnews and start the service.
set -eu
REPO=https://github.com/surya-sirisolla/Just-News.git
APP=/opt/justnews

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq python3-venv python3-pip git libmagic1 fonts-dejavu-core

id justnews >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/justnews --shell /usr/sbin/nologin justnews
if [ ! -d "$APP/.git" ]; then
  git clone "$REPO" "$APP"
fi
chown -R justnews:justnews "$APP"
runuser -u justnews -- python3 -m venv "$APP/.venv"
runuser -u justnews -- "$APP/.venv/bin/pip" install -q --upgrade pip
runuser -u justnews -- "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
install -d -o justnews -g justnews -m 700 "$APP/data" "$APP/logs"

cp "$APP/deploy/justnews.service" /etc/systemd/system/justnews.service
systemctl daemon-reload
systemctl enable justnews
echo "Installed. Add $APP/.env (chmod 600, owner justnews), then: sudo systemctl start justnews"
