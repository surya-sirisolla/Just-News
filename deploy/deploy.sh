#!/bin/sh
# Update to the latest main, install dependencies, run the tests, restart. Run as root
# (CI runs it through AWS Systems Manager). If the tests fail, the old version keeps running.
set -eu
APP=/opt/justnews
cd "$APP"
runuser -u justnews -- git fetch -q origin main
runuser -u justnews -- git reset -q --hard origin/main
runuser -u justnews -- "$APP/.venv/bin/pip" install -q -r requirements.txt
runuser -u justnews -- env JUSTNEWS_LOG_LEVEL=CRITICAL "$APP/.venv/bin/python" -m unittest discover -q tests
cp deploy/justnews.service /etc/systemd/system/justnews.service
systemctl daemon-reload
systemctl restart justnews
sleep 5
systemctl is-active justnews
echo "Deployed $(runuser -u justnews -- git log -1 --format='%h %s')"
