# Horizon deployment

This directory deploys Horizon commit
`9dfee928a6709b6586dbad7c65afc943a197b7dd` with a reproducible local patch.
The production timer is intentionally left disabled by `deploy.sh`; enable it only
after a successful manual run and public validation.

- Application: `/opt/dyyjs-horizon/app` (root-owned, read-only)
- Runtime data: `/opt/dyyjs-horizon/data`
- Jekyll source: `/opt/dyyjs-horizon/jekyll`
- Releases: `/opt/dyyjs-horizon/releases`
- Public symlink: `/var/www/dyyjs/horizon/current`
- Secret source: `/etc/dyyjs-news/env` (`DASHSCOPE_API_KEY`)
- Foreign fetch proxy: `HORIZON_FETCH_PROXY=http://127.0.0.1:7890`
- AI transport: direct, with `httpx.AsyncClient(trust_env=False)`

`caddy-horizon.txt` is a snippet for the existing site block. Do not change UFW.
