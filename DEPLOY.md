# Deploying BA Flow on a server (webhook / hybrid mode)

```
ClickUp ──HTTPS──► https://baflow.company.com/clickup/webhook
                      │  reverse proxy (TLS)            only this path is public
                      ▼
                   ba-flow serve  on 127.0.0.1:8080     one instance
                      │
                   data/ (SQLite state)   logs/   config/   templates/
```

> The Docker files and service unit below were written for this project but have not been run on a
> real server yet. Validate them on a test server first.

## 1. What IT needs to provide

| Item | Details |
|---|---|
| Server | Linux with Docker (preferred), or any Linux/Windows host with Python 3.12. 1 vCPU, 1 GB RAM, 5 GB disk is plenty. |
| Public HTTPS URL | A stable host name, e.g. `baflow.company.com`, with a valid TLS certificate. Only `POST /clickup/webhook` needs to be reachable from the internet. |
| Inbound | TCP 443 to the reverse proxy. Port 8080 must stay internal. Webhook requests are authenticated by an HMAC signature, not by source IP. |
| Outbound HTTPS | `api.clickup.com`, `*.clickup-attachments.com` (document downloads), `api.openai.com` (or `api.anthropic.com` if that provider is used). |
| Secrets | A place to keep `.env` on the server (bot ClickUp token, OpenAI key, webhook secret), readable only by the service account. |
| Backup | The `data/` folder (SQLite: context packs, processing steps, runs). |
| Monitoring (optional) | `GET http://127.0.0.1:8080/health` on the server: `200` = healthy, `503` = worker stopped. |

**Run exactly one instance.** The service uses SQLite and a single worker; two copies would process the same
events twice.

## 2. Files to copy to the server

Copy the project **without** `.venv/`, `data/`, `logs/`, `local_tests/`, `tests/`, `samples/` and `.env`.
Create `.env` on the server itself from `.env.example` (never send tokens by e-mail or chat):

```
CLICKUP_API_TOKEN=<bot user token>
OPENAI_API_KEY=<key>
CLICKUP_WEBHOOK_ID=        # filled in step 4
CLICKUP_WEBHOOK_SECRET=    # filled in step 4
```

In `config/settings.yaml` set:

```yaml
trigger:
  mode: "hybrid"
```

## 3a. Option A — Docker (recommended)

```bash
cd /opt/ba-flow
mkdir -p data logs
docker compose build
docker compose run --rm ba-flow ba-flow check-config
```

`docker-compose.yml` publishes the port on `127.0.0.1:8080` only, mounts `data/`, `logs/`, `config/` and
`templates/`, restarts automatically and has a health check. If the container user cannot write `data/` or `logs/`,
run `sudo chown -R 10001 data logs`.

## 3b. Option B — Linux without Docker

```bash
sudo useradd --system --home /opt/ba-flow baflow
cd /opt/ba-flow
python3.12 -m venv .venv
.venv/bin/pip install .
mkdir -p data logs && sudo chown -R baflow data logs
sudo -u baflow .venv/bin/ba-flow check-config
sudo cp deploy/ba-flow.service /etc/systemd/system/
```

The service starts in step 5. It runs `ba-flow serve --host 127.0.0.1` from `/opt/ba-flow`, so
`config/settings.yaml` is found in the working directory. Elsewhere, pass `--config` or set `BA_FLOW_CONFIG`.

## 3c. Option C — Windows Server

1. Install Python 3.12, then in the project folder: `py -3.12 -m venv .venv` and `.venv\Scripts\pip install .`
2. `.venv\Scripts\ba-flow check-config`
3. Run `.venv\Scripts\ba-flow.exe serve --host 127.0.0.1` as a Windows service with the project folder as the
   working directory: use your standard service wrapper (e.g. NSSM or WinSW), start automatically, restart on failure.
4. Reverse proxy: IIS (URL Rewrite + ARR) or Caddy for Windows, forwarding only `/clickup/webhook` to
   `http://127.0.0.1:8080`.

## 4. Reverse proxy and webhook registration

- Caddy: `deploy/Caddyfile` (automatic certificates). nginx: `deploy/nginx-ba-flow.conf`.
- Check that the webhook URL is reachable from outside before registering it.

Register the webhook once:

```bash
# Docker:
docker compose run --rm ba-flow ba-flow webhook register --url https://baflow.company.com/clickup/webhook
# without Docker (writes the values straight into .env):
.venv/bin/ba-flow webhook register --url https://baflow.company.com/clickup/webhook --save
```

With Docker, copy the printed `CLICKUP_WEBHOOK_ID` / `CLICKUP_WEBHOOK_SECRET` into `.env`.

## 5. Start and verify

```bash
docker compose up -d                 # or: sudo systemctl enable --now ba-flow
curl http://127.0.0.1:8080/health    # {"status": "ok", ...}
ba-flow webhook list                 # status=active, fail_count=0
```

Then do a real test in ClickUp: move a task in **US Intake** to `to analyze`. It should become `analyzing`
within about 10 seconds (the debounce).

## Operations

| Task | How |
|---|---|
| Logs | `logs/ba_flow.jsonl` (one JSON line per event, with `run_id`), or `docker compose logs -f` |
| Inspect a run | `ba-flow runs list`, `ba-flow runs show <run_id>` (the id is at the end of every bot comment) |
| Change settings or prompts | Edit `config/`, then restart (`docker compose restart` / `systemctl restart ba-flow`) and run `check-config` |
| Update the code | Copy the new version, then `docker compose up -d --build` / `.venv/bin/pip install . && systemctl restart ba-flow` |
| Webhook suspended by ClickUp | Fix the cause (service down, URL changed), then `ba-flow webhook reactivate <id>`. The hybrid safety poll catches the events it missed in the meantime. |
| URL changes | `ba-flow webhook delete <old id>`, then register again with the new URL |
| Stop using webhooks | `ba-flow webhook delete <id>`, set `trigger.mode: "polling"` and run `ba-flow run` instead of `serve` |
