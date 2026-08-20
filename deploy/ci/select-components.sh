#!/usr/bin/env bash
set -euo pipefail

before=${1:?before commit is required}
after=${2:?after commit is required}

if [[ "$before" =~ ^0+$ ]] || ! git cat-file -e "$before^{commit}" 2>/dev/null; then
  changed=$(git ls-tree -r --name-only "$after")
else
  changed=$(git diff --name-only "$before" "$after")
fi

components=()
add_component() {
  local candidate=$1 existing
  for existing in "${components[@]-}"; do
    [[ "$existing" == "$candidate" ]] && return
  done
  components+=("$candidate")
}

while IFS= read -r path; do
  case "$path" in
    z_my_server/webpage/*) add_component webpage ;;
    z_my_server/xiaoyu/VRChat-Category/*) add_component xiaoyu ;;
    z_my_server/status-dashboard/*) add_component status ;;
    z_my_server/news-pipeline/*) add_component news ;;
    z_my_server/youtube-relay/*) add_component youtube ;;
    z_my_server/bilibili-relay/*) add_component bilibili ;;
    z_my_server/horizon-deployment/*) add_component horizon ;;
    z_my_server/minecraft-server/*) add_component minecraft ;;
    deploy/server/*) add_component all ;;
  esac
done <<< "$changed"

IFS=,
printf '%s\n' "${components[*]-}"

