#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PEM="$DIR/../credentials/server_103_236_92_143_57423.pem"
SSH="ssh -p 57423 -i $PEM -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

$SSH root@103.236.92.143 'install -d -m 755 /opt/dyyjs-status/app /opt/dyyjs-status/state /var/www/dyyjs/status/assets /var/www/dyyjs/status/data'
rsync -avz --delete --exclude deploy.sh --exclude systemd --exclude __pycache__ \
  -e "$SSH" "$DIR/" root@103.236.92.143:/opt/dyyjs-status/app/
rsync -avz -e "$SSH" "$DIR/systemd/" root@103.236.92.143:/etc/systemd/system/
$SSH root@103.236.92.143 'install -m 644 /opt/dyyjs-status/app/index.html /var/www/dyyjs/status/index.html; install -m 644 /opt/dyyjs-status/app/assets/status.css /var/www/dyyjs/status/assets/status.css; install -m 644 /opt/dyyjs-status/app/assets/status.js /var/www/dyyjs/status/assets/status.js; systemctl daemon-reload; systemctl enable --now dyyjs-status-sampler.service dyyjs-status.timer; systemctl start dyyjs-status.service'
