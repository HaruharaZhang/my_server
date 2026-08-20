#!/usr/bin/env bash
set -euo pipefail

script=$(cd "$(dirname "$0")/.." && pwd)/server/deploy-my-server
bash -n "$script"
"$script" status >/dev/null
grep -q -- '--retry-all-errors' "$script"

if "$script" deploy invalid webpage >/dev/null 2>&1; then
  echo 'invalid SHA was accepted' >&2
  exit 1
fi
if "$script" deploy 0000000000000000000000000000000000000000 invalid >/dev/null 2>&1; then
  echo 'invalid component was accepted' >&2
  exit 1
fi

echo 'deployment contract tests passed'
