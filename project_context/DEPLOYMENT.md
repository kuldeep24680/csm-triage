# Deployment: Docker Compose on one EC2 server

Everything runs as four containers on one server. Scheduling, retries, recovery and alerts live in the app, so no other AWS services are needed.

**Before this:** finish [EC2_SETUP.md](EC2_SETUP.md). That gives you the server with Docker and a `<HOST>` name. The repo lives at **`/opt/csm-triage`**.

Allow about 25 minutes, mostly waiting for the image build and the first data load. Each step ends with a **Check**.

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

---

## Quick version

For when you've done this before. Each command is explained in steps 0–6 below.

```bash
ssh -i ~/.ssh/triage-key.pem ubuntu@<EIP>
cd /opt/csm-triage && git pull
cd deploy/docker && cp -n .env.example .env && nano .env && chmod 600 .env
docker compose config --quiet && echo OK
docker compose up -d --build
tmux new -s triage                                  # survives an SSH drop
docker compose exec api python -m triage bootstrap  # ~10 min, one time
# open https://<HOST>
```

---

## 0. Pre-flight on the server

```bash
ssh -i ~/.ssh/triage-key.pem ubuntu@<EIP>
docker run --rm hello-world >/dev/null && echo "docker OK"     # no sudo needed
docker compose version
free -h                                                        # ~7.6 GB RAM + 4 GB swap
df -h /                                                        # 20+ GB free
```

If `docker` says *permission denied*, log out and back in (EC2_SETUP step 8).

**Get the code into `/opt/csm-triage`.** Skip this if it's already there.

```bash
sudo mkdir -p /opt/csm-triage && sudo chown ubuntu:ubuntu /opt/csm-triage
git clone git@github.com:kuldeep24680/csm-triage.git /opt/csm-triage
```

**Check:** `ls /opt/csm-triage` shows `Dockerfile backend deploy frontend project_context …`.

---

## 1. Get the latest code

Push from your Mac first (`git push`), then on the server:

```bash
cd /opt/csm-triage
git pull
git log --oneline -1          # matches your latest commit on GitHub
ls Dockerfile deploy/docker/compose.yml deploy/docker/Caddyfile
```

**Check:** all three files are listed.

---

## 2. Configure secrets and settings

All remaining commands run in **`/opt/csm-triage/deploy/docker`**.

```bash
cd /opt/csm-triage/deploy/docker
cp -n .env.example .env       # -n: never overwrites an existing .env
```

Fill in the required values. These commands generate the secrets for you; replace `<HOST>` and the demo password:

```bash
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env
sed -i "s/^TRIAGE_JWT_SECRET=.*/TRIAGE_JWT_SECRET=$(openssl rand -hex 32)/" .env
sed -i "s/^SITE_HOST=.*/SITE_HOST=<HOST>/" .env                       # e.g. 13-233-10-20.sslip.io
sed -i "s/^TRIAGE_DEMO_PASSWORD=.*/TRIAGE_DEMO_PASSWORD=<choose-one>/" .env
nano .env                     # optional: OPENAI_API_KEY, TRIAGE_ALERT_WEBHOOK_URL
chmod 600 .env
```

| Variable | Required | Value |
|---|---|---|
| `POSTGRES_PASSWORD` | yes | Random (generated above). **Set once**: changing it later doesn't change the existing database's password |
| `TRIAGE_JWT_SECRET` | yes | Random, at least 32 characters. The API refuses to start with a weak one |
| `SITE_HOST` | yes | `<HOST>` from EC2_SETUP step 10, or your own domain |
| `TRIAGE_DEMO_PASSWORD` | yes | Password for all demo logins (managers and CSMs) |
| `OPENAI_API_KEY` | no | Empty means template briefs |
| `TRIAGE_ALERT_WEBHOOK_URL` | no | Slack incoming webhook for run failures |
| `TRIAGE_ENV` | keep `production` | Turns on the safety checks |
| `TRIAGE_USE_WALL_CLOCK` | keep `false` | Demo data ends 2026-10-16; runs keep scoring that date |

Keep comments on their own line: `KEY=value  # note` would make `# note` part of the value.

**Check:**

```bash
docker compose config --quiet && echo OK
grep -c CHANGE_ME .env        # must print 0
```

---

## 3. Build and start (about 5 minutes the first time)

```bash
docker compose up -d --build
```

This pulls `postgres:16` and `caddy:2`, builds the app image (web build + Python dependencies), then starts `db` → `api` → `scheduler` → `caddy`, each waiting for the previous one to be healthy.

```bash
docker compose ps
docker compose logs api | tail -5
```

**Check:**
- `db` and `api` show **(healthy)**; `scheduler` shows **(healthy)** within a few minutes; `caddy` is **Up**.
- `docker compose logs api` ends with `Application startup complete`, and does **not** say `refusing to start in production`. If it does, fix the setting it names in `.env`, then `docker compose up -d`.

---

## 4. Load data and train (one time, about 10 minutes)

Run it inside `tmux`, so an SSH disconnect doesn't kill it:

```bash
tmux new -s triage            # later: Ctrl-b d to detach, `tmux attach -t triage` to return
cd /opt/csm-triage/deploy/docker
docker compose exec api python -m triage bootstrap
```

This creates the schema, generates the synthetic data (~1.2M records), ingests it, trains the model, replays 8 weekly runs for history and creates the logins. Re-running it is safe: each step is skipped if its result already exists.

**Check:** the output ends with `bootstrap: done`. Then:

```bash
docker compose exec api python -m triage run --tenant quill --no-retry | grep '"status"' | head -1
```

That should print `"status": "succeeded"`.

---

## 5. Open the app

Go to `https://<HOST>`:

- [ ] Padlock shows a valid certificate. The first request can take ~20 seconds while Caddy obtains it; `docker compose logs caddy | grep -i certificate` shows progress.
- [ ] Sign in as `manager@northwind.example` with your `TRIAGE_DEMO_PASSWORD`, and the call lists load.
- [ ] Pick a CSM, then open an account: the brief, "Why" bars and recent activity show; clicking a signal opens its source rows.
- [ ] **Agent runs → Run now** starts a run, which shows **succeeded** after about 20 seconds.
- [ ] Swagger works at `https://<HOST>/api/docs`.
- [ ] From your Mac: `curl -s https://<HOST>/api/health | jq .ok` returns `true`.

Every login uses the same `TRIAGE_DEMO_PASSWORD`. To list them (one manager per company, plus each CSM):

```bash
docker compose exec -T db psql -U triage -d triage -c "SELECT tenant_id, role, email FROM users ORDER BY 1, 2, 3"
```

---

## 6. Prove the unattended path

```bash
docker compose exec scheduler python -m triage scheduler --dry-run
```

This prints the next fire time for each job, in UTC: quill 20:30, helios 01:00 or 02:00 (depending on UK summer time), northwind 06:00 or 07:00, watchdog 06:30.

```bash
docker compose logs scheduler | tail
```

This shows `scheduler started` and `next job …`.

To see a scheduled run now instead of at 02:00, set `TRIAGE_SCHEDULE_RUN_AT` in `.env` to 2 minutes from now **in that tenant's local time** (quill = India time), then:

```bash
docker compose up -d scheduler          # restarts it with the new setting
docker compose logs -f scheduler        # wait for "job end run:quill rc=0", then Ctrl-c
sed -i "s/^TRIAGE_SCHEDULE_RUN_AT=.*/TRIAGE_SCHEDULE_RUN_AT=02:00/" .env
docker compose up -d scheduler          # back to 02:00
```

**Check:**
- **Agent runs** in the app shows a run with trigger **schedule**.
- The next morning, the overnight runs are there without you doing anything.

---

## Day-2 operations

| Task | Command (in `/opt/csm-triage/deploy/docker`) |
|---|---|
| Deploy a new version | `git -C /opt/csm-triage pull && docker compose up -d --build`. The api applies new migrations on start |
| Status | `docker compose ps` |
| Logs | `docker compose logs -f api` / `scheduler` (JSON lines with trace_id, user_name, run_id) |
| A user complains | `GET /api/audit/requests?user_name=<name>` → copy `trace_id` → `docker compose logs api \| grep <trace_id>` |
| Run now / resume | App: **Agent runs → Run now / Resume** (managers). CLI: `docker compose exec api python -m triage run --tenant <t>` or `run --resume <run_id>` |
| Any CLI command | `docker compose exec api python -m triage <command>` |
| Retrain | `docker compose exec api python -m triage train` (used from the next run) |
| Backup DB | `docker compose exec -T db pg_dump -U triage triage \| gzip > ~/triage-$(date +%F).sql.gz` |
| Restore DB | `gunzip -c <file> \| docker compose exec -T db psql -U triage triage` |
| Changed `Caddyfile` | `docker compose restart caddy` (Caddy doesn't reload the mounted file on its own) |
| Changed `.env` | `docker compose up -d` (recreates the containers that use it) |
| Restart everything | `docker compose restart` |
| Stop (keep data) | `docker compose down` |
| Wipe everything | `docker compose down -v`. **Deletes the database and data volumes** |

Containers restart automatically (`restart: unless-stopped`), including after a server reboot. Docker starts on boot.

## Production safeguards (already on)

| Safeguard | Detail |
|---|---|
| Refuses weak secrets | With `TRIAGE_ENV=production`, the api won't start if `TRIAGE_JWT_SECRET` is shorter than 32 characters or still a placeholder, or if the DB password is the development default |
| Sign-in throttling | 10 failed sign-ins for one email, or 50 from one IP, within 15 minutes → `429` for 15 minutes |
| No demo password in production | The sign-in page and Swagger don't show `triage-demo`; demo buttons only fill the email |
| Security headers | HSTS, `nosniff`, `X-Frame-Options: DENY`, referrer and permissions policy; `Server` header removed |
| Not exposed | Postgres and the API port are reachable only inside Docker; only Caddy publishes 80/443 |
| Non-root | App containers run as user `triage` (uid 10001) |
| Runs isolated from the API | "Run now" and "Resume" start the run as a separate process, so the API stays responsive and memory is released afterwards |
| Health checks | `db` (pg_isready), `api` (`/api/health` every 30s, not written to the audit table), `scheduler` (DB reachable and job plan computes, every 5 min) |
| Log rotation | Every container's logs are capped at 5 × 20 MB |

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
| `api` exits with "refusing to start in production" | Fix the named setting in `.env` (see Production safeguards), then `docker compose up -d` |
| Sign-in says "too many failed sign-in attempts" | Wait 15 minutes, or `docker compose restart api` to clear the counter |
| Build is killed or very slow | Too little memory. Use `t3.large`, and check swap is on (`free -h`) |

## Alternative without Docker

The systemd units in `deploy/ec2/` (see its README) run the same app directly on the host. Use one or the other, not both.
