#!/usr/bin/env bash
set -euo pipefail

script=$(cd "$(dirname "$0")/.." && pwd)/server/deploy-my-server
workflow=$(cd "$(dirname "$0")/../.." && pwd)/.github/workflows/deploy.yml
bash -n "$script"
"$script" status >/dev/null
grep -q -- '--retry-all-errors' "$script"
! grep -q 'api.github.com/zen' "$script"
grep -q 'require_listener 8090' "$script"
grep -q 'require_listener 8091' "$script"
grep -q '/opt/dyyjs-youtube/app/requirements.txt' "$script"
grep -q '/opt/dyyjs-youtube/app/smoke_test.py' "$script"
grep -q 'disable --now dyyjs-youtube-update.timer' "$script"
grep -q 'npm run check:content; npm run build' "$script"
grep -q '/opt/dyyjs-webpage/releases/' "$script"
grep -q 'webpage build contains unexpected top-level output' "$script"
grep -q 'curl_status /manifest.webmanifest 200' "$script"
grep -q 'curl_status /assets/archive-cassette.glb 404' "$script"
grep -q 'bootstrap=true' "$workflow"
grep -q 'deploy-my-server deploy "$SHA" deployment' "$workflow"
grep -q 'deploy-my-server deploy "$SHA" "$joined"' "$workflow"
! grep -qE 'ss -ltn \| grep -qE' "$script"
grep -A40 '^restore_transaction()' "$script" | grep -q 'exit 1'

if "$script" deploy invalid webpage >/dev/null 2>&1; then
  echo 'invalid SHA was accepted' >&2
  exit 1
fi
if "$script" deploy 0000000000000000000000000000000000000000 invalid >/dev/null 2>&1; then
  echo 'invalid component was accepted' >&2
  exit 1
fi

echo 'deployment contract tests passed'
