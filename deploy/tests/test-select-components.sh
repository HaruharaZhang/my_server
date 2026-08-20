#!/usr/bin/env bash
set -euo pipefail

script=$(cd "$(dirname "$0")/.." && pwd)/ci/select-components.sh
repo=$(mktemp -d)
trap 'rm -rf "$repo"' EXIT
cd "$repo"
git init -q
git config user.name test
git config user.email test@example.invalid

mkdir -p \
  z_my_server/{webpage,xiaoyu/VRChat-Category,status-dashboard,news-pipeline,youtube-relay,bilibili-relay,horizon-deployment,minecraft-server} \
  deploy/server
for path in \
  z_my_server/webpage/index.html \
  z_my_server/xiaoyu/VRChat-Category/index.html \
  z_my_server/status-dashboard/app.py \
  z_my_server/news-pipeline/app.py \
  z_my_server/youtube-relay/app.py \
  z_my_server/bilibili-relay/app.py \
  z_my_server/horizon-deployment/install.sh \
  z_my_server/minecraft-server/server.sh \
  deploy/server/deploy-my-server \
  deploy/server/VERSION; do
  printf 'initial\n' > "$path"
done
git add .
git commit -qm initial
first=$(git rev-parse HEAD)

state=$(mktemp)
result=$("$script" "$first" "$state")
expected=webpage,xiaoyu,status,news,youtube,bilibili,horizon,minecraft,deployment
[[ "$result" == "$expected" ]]

for component in webpage xiaoyu status news youtube bilibili horizon minecraft deployment; do
  printf '%s\t%s\n' "$component" "$first" >> "$state"
done
printf 'changed\n' >> z_my_server/news-pipeline/app.py
git add .
git commit -qm news
second=$(git rev-parse HEAD)
[[ "$("$script" "$second" "$state")" == news ]]

sed -i.bak 's/initial/updated/' deploy/server/VERSION
rm deploy/server/VERSION.bak
git add .
git commit -qm deployment
third=$(git rev-parse HEAD)
sed -i.bak "s/$first/$second/g" "$state"
rm "$state.bak"
[[ "$("$script" "$third" "$state")" == deployment ]]

awk '$1 != "news"' "$state" > "$state.next"
mv "$state.next" "$state"
[[ "$("$script" "$third" "$state")" == news,deployment ]]

echo 'component selector tests passed'
