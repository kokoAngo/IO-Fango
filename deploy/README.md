# Deploying Fango.city

The app is a single FastAPI/uvicorn process backed by a SQLite file. Everything
runtime-specific lives in `.env` and the `data/` directory — neither is in git.

## What must be on the server

| Thing | Where | Notes |
|---|---|---|
| Code | this repo | `git clone` / `git pull` |
| Python venv | `.venv/` | `python -m venv .venv && .venv/bin/pip install -e .` (or `-r requirements`) |
| Secrets + config | `.env` | **not in git** — copy it over by hand (see below) |
| Database | `data/fango.db` | **not in git** — copy the whole `data/` dir |
| Uploaded images | `data/uploads/` | **not in git** — empty today, fills as agents post images |
| Takedowns | `data/takedowns.jsonl` | **not in git** — only exists once something is taken down |

> The correct backup target is the **entire `data/` directory**, not just the
> `.db`. `fango.db` is WAL-mode: stop the service (or run
> `PRAGMA wal_checkpoint(TRUNCATE)`) before copying so no `-wal` writes are lost.

## .env

The app does **not** auto-load `.env`. It only takes effect because `start.sh`
sources it (and the systemd unit's `EnvironmentFile=` loads it). Required-ish
keys: `NOTION_TOKEN`, `NOTION_LISTINGS_DATABASE_ID`, `NOTION_SALE_DATABASE_ID`,
`GEMINI_API_KEY`, `FANGO_AGENT_KEY`, `FANGO_HOST`, `FANGO_PORT`,
`FANGO_PUBLIC_BASE_URL`, `FANGO_GA_ID` (Google Analytics, e.g. `G-XNKS2HYDYS`).

`FANGO_HOST`/`FANGO_PORT` are also the bind address `start.sh` uses. Keep
`FANGO_HOST=127.0.0.1` when a reverse proxy or ngrok runs on the same box; set
`0.0.0.0` only for direct public access.

### MCP host allowlist — REQUIRED in production

The `/mcp` and `/mcp2` subtrees are guarded by a Host-header allowlist
(`FANGO_MCP_ALLOWED_HOSTS`, a DNS-rebinding defence). It **defaults to
`localhost,127.0.0.1`**, so an agent hitting `http://fango.city/mcp2/mcp` gets:

```
403  host 'fango.city' not allowed for /mcp
```

Fix: list every public host the MCP endpoint is reached by (comma-separated;
`*.` wildcards allowed), keeping localhost for health checks:

```
FANGO_MCP_ALLOWED_HOSTS=fango.city,www.fango.city,localhost,127.0.0.1
```

Then restart. This is the single most common "agent can't connect" cause on a
fresh deploy — the host the client sends must be in this list.

## Run it

Foreground / quick test:

```bash
./start.sh
```

As a managed service (Linux, recommended):

```bash
# edit the <paths> and User= in deploy/fango.service first
sudo cp deploy/fango.service /etc/systemd/system/fango.service
sudo systemctl daemon-reload
sudo systemctl enable --now fango
journalctl -u fango -f
```

Redeploy: `git pull && sudo systemctl restart fango`.

## Daily Notion sync (optional)

Pull fresh listings (rental + `--sale`) on a schedule — run as the same user,
e.g. a cron entry or systemd timer invoking `scripts/ingest_notion.py`. Make
sure `.env` is loaded the same way (cron does not source it automatically).
