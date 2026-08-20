#!/usr/bin/env bash
set -euo pipefail

SOURCE_COMMIT=9dfee928a6709b6586dbad7c65afc943a197b7dd
BASE=/opt/dyyjs-horizon
STAGING=/opt/dyyjs-horizon-deploy
PROXY=http://127.0.0.1:7890

if [[ $(id -u) -ne 0 ]]; then
    echo "install-server.sh must run as root" >&2
    exit 1
fi
test -f "$STAGING/patches/0001-dyyjs-model-router.patch"
test -f /etc/dyyjs-news/env
for command in git uv bundle ruby; do
    command -v "$command" >/dev/null || {
        echo "missing bootstrap dependency: $command" >&2
        exit 1
    }
done
if systemctl is-active --quiet dyyjs-horizon.service; then
    echo "dyyjs-horizon.service is running; retry after the oneshot completes" >&2
    exit 1
fi

timer_enabled=$(systemctl is-enabled dyyjs-horizon.timer 2>/dev/null || true)
timer_active=$(systemctl is-active dyyjs-horizon.timer 2>/dev/null || true)

id horizon >/dev/null 2>&1 || useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin horizon
install -d -o root -g root -m 0755 "$BASE"
install -d -o horizon -g horizon -m 0750 \
    "$BASE/data" "$BASE/jekyll" "$BASE/logs" "$BASE/state" "$BASE/bundle"
install -d -o horizon -g horizon -m 0755 "$BASE/releases"

candidate=$(mktemp -d /opt/horizon-candidate.XXXXXX)
cleanup_candidate() {
    [[ -d "$candidate" ]] && rm -rf "$candidate"
}
trap cleanup_candidate EXIT
git -c http.proxy="$PROXY" clone --filter=blob:none --no-checkout \
    https://github.com/Thysrael/Horizon.git "$candidate"
git -C "$candidate" config http.proxy "$PROXY"
git -C "$candidate" fetch --depth=1 origin "$SOURCE_COMMIT"
git -C "$candidate" checkout --detach "$SOURCE_COMMIT"
test "$(git -C "$candidate" rev-parse HEAD)" = "$SOURCE_COMMIT"
git -C "$candidate" apply --check "$STAGING/patches/0001-dyyjs-model-router.patch"
git -C "$candidate" apply "$STAGING/patches/0001-dyyjs-model-router.patch"

HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" \
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv sync \
    --project "$candidate" --frozen --extra dev
(cd "$candidate" && .venv/bin/python -m pytest -q tests)
"$candidate/.venv/bin/python" -m compileall -q "$candidate/src"

rm -rf "$BASE/app.new"
mv "$candidate" "$BASE/app.new"
chown -R root:root "$BASE/app.new"
chmod -R a-w "$BASE/app.new"
chmod 0555 "$BASE/app.new"

if [[ ! -f "$BASE/jekyll/_config.yml" ]]; then
    cp -a "$BASE/app.new/docs/." "$BASE/jekyll/"
fi
chown horizon:horizon "$BASE/jekyll"
chmod 0750 "$BASE/jekyll"
install -o horizon -g horizon -m 0640 "$STAGING/config/config.json" "$BASE/data/config.json"
install -o horizon -g horizon -m 0644 "$STAGING/jekyll/_config.production.yml" "$BASE/jekyll/_config.production.yml"
install -o horizon -g horizon -m 0644 "$STAGING/jekyll/Gemfile" "$BASE/jekyll/Gemfile"
install -o horizon -g horizon -m 0644 "$STAGING/jekyll/Gemfile.lock" "$BASE/jekyll/Gemfile.lock"

rm -rf "$BASE/profiles.new"
install -d -o root -g root -m 0755 "$BASE/profiles.new"
cp -a "$BASE/app.new/profiles/tech-news" "$BASE/profiles.new/"
cp -a "$BASE/app.new/profiles/tech-blog" "$BASE/profiles.new/"
chown -R root:root "$BASE/profiles.new"

su -s /bin/bash horizon -c \
    "HTTP_PROXY=$PROXY HTTPS_PROXY=$PROXY BUNDLE_GEMFILE=$BASE/jekyll/Gemfile bundle config set --local path $BASE/bundle"
su -s /bin/bash horizon -c \
    "HTTP_PROXY=$PROXY HTTPS_PROXY=$PROXY BUNDLE_GEMFILE=$BASE/jekyll/Gemfile bundle install"

rm -rf "$BASE/app.previous" "$BASE/profiles.previous"
if [[ -d "$BASE/app" ]]; then mv "$BASE/app" "$BASE/app.previous"; fi
mv "$BASE/app.new" "$BASE/app"
if [[ -d "$BASE/profiles" ]]; then mv "$BASE/profiles" "$BASE/profiles.previous"; fi
mv "$BASE/profiles.new" "$BASE/profiles"

install -o root -g root -m 0755 "$STAGING/scripts/run-dyyjs-horizon" /usr/local/sbin/run-dyyjs-horizon
install -o root -g root -m 0644 "$STAGING/systemd/dyyjs-horizon.service" /etc/systemd/system/dyyjs-horizon.service
install -o root -g root -m 0644 "$STAGING/systemd/dyyjs-horizon.timer" /etc/systemd/system/dyyjs-horizon.timer
systemctl daemon-reload
if [[ "$timer_enabled" == enabled ]]; then
    systemctl enable dyyjs-horizon.timer >/dev/null
else
    systemctl disable dyyjs-horizon.timer >/dev/null 2>&1 || true
fi
if [[ "$timer_active" == active ]]; then
    systemctl restart dyyjs-horizon.timer
else
    systemctl stop dyyjs-horizon.timer
fi

trap - EXIT
echo "Horizon installed at $SOURCE_COMMIT; timer state preserved"
