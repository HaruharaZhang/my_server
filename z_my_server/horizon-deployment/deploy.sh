#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PEM="$DIR/../credentials/server_103_236_92_143_57423.pem"
REMOTE=/opt/dyyjs-horizon-deploy

rsync -avz --delete \
  -e "ssh -p 57423 -i $PEM -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
  "$DIR/" root@103.236.92.143:"$REMOTE/"

ssh -p 57423 -i "$PEM" -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@103.236.92.143 "$REMOTE/scripts/install-server.sh"
