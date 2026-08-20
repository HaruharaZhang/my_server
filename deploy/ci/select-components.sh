#!/usr/bin/env bash
set -euo pipefail

target=${1:?target commit is required}
state_file=${2:?component state file is required}

git cat-file -e "$target^{commit}" 2>/dev/null || {
  echo "target commit is unavailable: $target" >&2
  exit 2
}
[[ -f "$state_file" ]] || {
  echo "component state file is unavailable: $state_file" >&2
  exit 2
}

components=(webpage xiaoyu status news youtube bilibili horizon minecraft deployment)

component_paths() {
  case "$1" in
    webpage) printf '%s\n' 'z_my_server/webpage/' ;;
    xiaoyu) printf '%s\n' 'z_my_server/xiaoyu/VRChat-Category/' ;;
    status) printf '%s\n' 'z_my_server/status-dashboard/' ;;
    news) printf '%s\n' 'z_my_server/news-pipeline/' ;;
    youtube) printf '%s\n' 'z_my_server/youtube-relay/' ;;
    bilibili) printf '%s\n' 'z_my_server/bilibili-relay/' ;;
    horizon) printf '%s\n' 'z_my_server/horizon-deployment/' ;;
    minecraft) printf '%s\n' 'z_my_server/minecraft-server/' ;;
    deployment)
      printf '%s\n' 'deploy/server/deploy-my-server' 'deploy/server/VERSION'
      ;;
  esac
}

state_for() {
  awk -v component="$1" '$1 == component { print $2; exit }' "$state_file"
}

selected=()
for component in "${components[@]}"; do
  base=$(state_for "$component")
  if [[ -z "$base" ]] || ! git cat-file -e "$base^{commit}" 2>/dev/null; then
    selected+=("$component")
    continue
  fi
  [[ "$base" == "$target" ]] && continue

  mapfile -t paths < <(component_paths "$component")
  if git diff --quiet "$base" "$target" -- "${paths[@]}"; then
    continue
  fi
  selected+=("$component")
done

IFS=,
printf '%s\n' "${selected[*]-}"
