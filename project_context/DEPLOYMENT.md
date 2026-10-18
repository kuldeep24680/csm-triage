# Deployment: Docker Compose on one EC2 server

Everything runs as four containers on one server. Scheduling, retries, recovery and alerts live in the app, so no other AWS services are needed.

**Before this:** finish [EC2_SETUP.md](EC2_SETUP.md). That gives you the server, Docker, the cloned repo, a `<HOST>` name and the secrets.

Allow about 20 minutes, mostly waiting for the image build and the first data load. Each step ends with a **Check**.

```
Internet ─443─> caddy (TLS) ──> api :8000 (API + web app) ──> db (Postgres 16)
                                scheduler ── nightly run per tenant (02:00 tenant-local)
                                          └─ watchdog (06:30 UTC)
```

| Container | What it does | Data |
|---|---|---|
| `db` | Postgres 16, not reachable from outside | volume `pgdata` |
| `api` | Applies new DB migrations on start, then serves the API, Swagger and web app | volume `landing` (synthetic source files) |
| `scheduler` | `python -m triage scheduler`: each tenant's nightly run plus the morning watchdog. Each job runs as a child process | same image as api |
| `caddy` | HTTPS with an automatic Let's Encrypt certificate, ports 80 and 443 | volumes `caddy_data`, `caddy_config` |

All commands below run on the server, in `~/csm-triage/deploy/docker`.

---

## 1. Configure

```bash
cd ~/csm-triage && git pull
cd deploy/docker
cp .env.example .env
nano .env
```

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -hex 16` |
| `TRIAGE_JWT_SECRET` | `openssl rand -hex 32` |
| `SITE_HOST` | `<HOST>` from EC2_SETUP step 10, e.g. `13-233-10-20.sslip.io` |
| `TRIAGE_DEMO_PASSWORD` | Password for the demo logins |
| `OPENAI_API_KEY` | Optional; empty means template briefs |
| `TRIAGE_ALERT_WEBHOOK_URL` | Optional Slack webhook |

Keep comments on their own line: `KEY=value  # note` would make `# note` part of the value.

```bash
chmod 600 .env
```

**Check:** `docker compose config --quiet && echo OK` prints `OK`. A missing required value makes it fail and names the variable.

---

## 2. Build and start (about 5 minutes the first time)

```bash
docker compose up -d --build
```

**Check:** `docker compose ps` shows `db`, `api`, `scheduler` and `caddy` all **running**, with `api` **(healthy)** after about 30 seconds.

---

## 3. Load data and train (one time, about 10 minutes)

```bash
docker compose exec api python -m triage bootstrap
```

This migrates, generates the synthetic data (~1.2M records), ingests it, trains the model, replays 8 weekly runs for history and creates the logins. Re-running it is safe: each step is skipped if its result already exists.

**Check:** the output ends with `bootstrap: done`. Then:

```bash
docker compose exec api python -m triage run --tenant quill --no-retry | tail -5
```

That should show `"status": "succeeded"`.

---

## 4. Open the app

Go to `https://<HOST>`:

- [ ] Padlock shows a valid certificate. The first request can take ~20 seconds while Caddy obtains it.
- [ ] Log in as `manager@northwind.example` with your demo password, and the call list loads.
- [ ] An account page opens quickly, with the brief and timeline.
- [ ] Swagger works at `https://<HOST>/api/docs`.
- [ ] `curl -s https://<HOST>/api/health | jq .ok` returns `true`.

---

## 5. Prove the unattended path

```bash
docker compose exec scheduler python -m triage scheduler --dry-run
```

This prints the next fire time for each job, in UTC: quill 20:30, helios 01:00 or 02:00 (depending on UK summer time), northwind 06:00 or 07:00, watchdog 06:30.

```bash
docker compose logs scheduler | tail
```

This shows `scheduler started` and `next job …`.

To see a scheduled run without waiting for 02:00, set `TRIAGE_SCHEDULE_RUN_AT` in `.env` to 2 minutes from now (tenant-local time), then:

```bash
docker compose up -d scheduler          # restart with the new setting
docker compose logs -f scheduler        # wait for "job end run:<tenant> rc=0"
```

Afterwards, set it back to `02:00` and run `docker compose up -d scheduler` again.

**Check:**
- **Agent runs** in the app shows runs with trigger **schedule**.
- The next morning, the overnight runs are there without you doing anything.

---

## Day-2 operations

| Task | Command (in `deploy/docker`) |
|---|---|
| Deploy a new version | `git pull && docker compose up -d --build`. The api applies new migrations on start |
| Status | `docker compose ps` |
| Logs | `docker compose logs -f api` / `scheduler` (JSON lines with trace_id, user_name, run_id) |
| A user complains | `GET /api/audit/requests?user_name=<name>` → copy `trace_id` → `docker compose logs api \| grep <trace_id>` |
| Run now / resume | App: **Agent runs → Run now / Resume** (managers). CLI: `docker compose exec api python -m triage run --tenant <t>` or `run --resume <run_id>` |
| Any CLI command | `docker compose exec api python -m triage <command>` |
| Retrain | `docker compose exec api python -m triage train` (used from the next run) |
| Backup DB | `docker compose exec -T db pg_dump -U triage triage \| gzip > ~/triage-$(date +%F).sql.gz` |
| Restore DB | `gunzip -c <file> \| docker compose exec -T db psql -U triage triage` |
| Restart everything | `docker compose restart` |
| Stop (keep data) | `docker compose down` |
| Wipe everything | `docker compose down -v`. **Deletes the database and data volumes** |

Containers restart automatically (`restart: unless-stopped`), including after a server reboot. Docker starts on boot.

## How failures are handled

| Failure | What happens |
|---|---|
| A nightly run fails | It resumes once automatically from the failed step. If that also fails, an alert is sent and the run waits for a manager to **Resume** |
| A run is killed (out of memory, timeout) | The scheduler alerts. The watchdog marks the run `abandoned` and starts the missing run |
| Server down at 02:00 | The 06:30 UTC watchdog starts any missing run |
| api crashes | Docker restarts it; Caddy keeps serving once it's back |
| Two runs for one tenant | Prevented by a Postgres advisory lock |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker compose config` says `set POSTGRES_PASSWORD` (or another variable) | Fill it in `deploy/docker/.env` |
| Browser shows a certificate error | Ports 80 and 443 must be open in the security group, and `SITE_HOST` must resolve to the Elastic IP. See `docker compose logs caddy` |
| `api` stays unhealthy | `docker compose logs api`, usually a wrong `.env` value. Changing `POSTGRES_PASSWORD` after the first start doesn't change the existing DB's password; use `docker compose down -v` to start over (this wipes data) |
| Login fails right after deploy | `bootstrap` hasn't run yet (step 3) |
| Build is killed or very slow | Too little memory. Use `t3.large`, and check swap is on (`free -h`) |

## Alternative without Docker

The systemd units in `deploy/ec2/` (see its README) run the same app directly on the host. Use one or the other, not both.
