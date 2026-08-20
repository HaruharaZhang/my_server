#!/usr/bin/env bash
# 把本地 news-pipeline 源码推到服务器 /opt/dyyjs-news/app/。
# 改代码永远改本地再运行本脚本，服务器上的副本不手改。
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PEM="$DIR/../credentials/server_103_236_92_143_57423.pem"

rsync -avz --delete \
  --exclude 'deploy.sh' \
  --exclude '__pycache__' \
  -e "ssh -p 57423 -i $PEM -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
  "$DIR/" root@103.236.92.143:/opt/dyyjs-news/app/

echo "deployed to /opt/dyyjs-news/app/"
