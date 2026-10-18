# Deployment: one EC2 server

Everything runs on a single Ubuntu EC2 instance: Postgres, the API (which also serves the web app), the nightly agent runs, the watchdog and alerts. Scheduling, retries and recovery live in the app and systemd; no other AWS services are needed.

Allow about 45 minutes. Each step ends with a **Check**; don't move on until it passes.

```
Internet ──443──> Caddy (TLS) ──> uvicorn :8000 (API + web app) ──> Postgres :5432
                                   systemd timers ──> triage run --tenant X (nightly, per tenant)
                                                  ──> triage watchdog (06:30 UTC)
```

---

## 0. Before you start

| You need | Notes |
|---|---|
| AWS account + an EC2 key pair (`.pem`) | Region close to you, e.g. `ap-south-1` (Mumbai) |
| GitHub access to `kuldeep24680/csm-triage` | The repo is private: you'll add a read-only deploy key in step 3 |
| OpenAI API key | Optional. Without it briefs use the template writer |
| Slack incoming-webhook URL | Optional. Without it alerts go to the log only |

---

## 1. Launch the instance

In the EC2 console, choose **Launch instance** and set:

| Setting | Value |
|---|---|
| AMI | Ubuntu Server 24.04 LTS (x86_64) |
| Type | `t3.large` (2 vCPU, 8 GB). Training and the first data load need the memory. You can drop to `t3.medium` afterwards |
| Storage | 30 GB gp3 |
| Security group | SSH 22 from **My IP** only; HTTP 80 and HTTPS 443 from anywhere |
| Key pair | yours |

Then allocate an **Elastic IP** and associate it with the instance, so the address survives a stop or start.

```bash
ssh -i ~/.ssh/<key>.pem ubuntu@<ELASTIC_IP>
```

**Check:** you get a shell prompt.

---

## 2. Install system packages

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3.12 python3.12-venv python3-pip postgresql git make jq
# Node 22 (the frontend build needs Node 20.19+ or 22.12+)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt -y install nodejs
# Caddy (TLS reverse proxy)
sudo apt -y install debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt -y install caddy
```

**Check:** `python3.12 --version` (3.12.x), `node --version` (v22.x), `psql --version` (16.x), `caddy version`.

---

## 3. Get the code

Create a service user, then a deploy key so the server can read the private repo:

```bash
sudo useradd --system --create-home --home-dir /opt/triage --shell /bin/bash triage
sudo -u triage mkdir -p -m 700 /opt/triage/.ssh
sudo -u triage ssh-keygen -t ed25519 -N "" -f /opt/triage/.ssh/id_ed25519
sudo cat /opt/triage/.ssh/id_ed25519.pub
```

On GitHub, go to repo **Settings → Deploy keys → Add deploy key**, paste the key and leave write access **off**. Then:

```bash
sudo -u triage bash -c 'ssh-keyscan github.com >> ~/.ssh/known_hosts && \
  git clone git@github.com:kuldeep24680/csm-triage.git /opt/triage/app'
```

> Note: the code lives in `/opt/triage/app`. The systemd unit files assume `/opt/triage`, so step 8 rewrites the paths.

**Check:** `ls /opt/triage/app` shows `backend frontend deploy …`.

---

## 4. Database

```bash
DBPASS=$(openssl rand -hex 16); echo "DB password: $DBPASS"   # keep it for step 6
sudo -u postgres psql -c "CREATE ROLE triage LOGIN CREATEROLE PASSWORD '$DBPASS';"
sudo -u postgres psql -c "CREATE DATABASE triage OWNER triage;"
```

- `triage` owns the schema. It needs `CREATEROLE` only so the migration can create the restricted `triage_app` role that row-level security runs under.
- Postgres listens on localhost only (the Ubuntu default), so nothing else is exposed.

**Check:** `PGPASSWORD=$DBPASS psql -h localhost -U triage -d triage -c 'select 1'` returns `1`.

---

## 5. Install the app

```bash
sudo -u triage bash -c 'cd /opt/triage/app && python3.12 -m venv .venv && \
  .venv/bin/pip install -q -r backend/requirements.txt && \
  cd frontend && npm ci && npm run build'
```

**Check:** `ls /opt/triage/app/frontend/dist/index.html` exists. The API serves this folder, so no separate web server is needed for the frontend.

---

## 6. Configuration and secrets

```bash
sudo mkdir -p /etc/triage
sudo cp /opt/triage/app/deploy/ec2/triage.env.example /etc/triage/triage.env
sudo nano /etc/triage/triage.env
```

Set these values:

| Variable | Value |
|---|---|
| `TRIAGE_DATABASE_URL` | `postgresql://triage:<DBPASS>@localhost:5432/triage` |
| `TRIAGE_JWT_SECRET` | output of `openssl rand -hex 32` |
| `OPENAI_API_KEY` | your key, or leave empty |
| `TRIAGE_ALERT_WEBHOOK_URL` | Slack webhook, or leave empty |
| `TRIAGE_USE_WALL_CLOCK` | keep `false` for the demo (see the note in the file) |

Lock the file down:

```bash
sudo chown root:triage /etc/triage/triage.env && sudo chmod 640 /etc/triage/triage.env
```

Make a helper so manual commands see the same settings as systemd:

```bash
sudo tee /usr/local/bin/triage >/dev/null <<'EOF'
#!/bin/bash
# Run a triage CLI command as the triage user with the production settings.
exec sudo -u triage bash -c 'set -a; source /etc/triage/triage.env; set +a; cd /opt/triage/app/backend && ../.venv/bin/python -m triage "$@"' _ "$@"
EOF
sudo chmod +x /usr/local/bin/triage
```

**Check:** `triage --help` lists `migrate, generate, ingest, train, run, watchdog, …`.

---

## 7. Load data and train (about 10 minutes)

```bash
triage migrate              # creates schema, RLS policies, indexes
triage generate             # synthetic landing zone, ~1.2M records (~1 min)
triage ingest               # load + resolve identities, all tenants
triage train                # fit, evaluate, register the model (~3 min)
triage backfill --weeks 8   # 8 weekly runs so "what changed" and sparklines have history
triage seed-users --password '<choose-a-demo-password>'
```

- Step 7 is a one-time load. After this, the nightly timers keep scores fresh.
- `seed-users` prints the logins: one manager per tenant (`manager@<tenant>.example`) plus one per CSM.

**Check:** `triage run --tenant quill --no-retry` ends with `"status": "succeeded"`.

---

## 8. Install the services

The unit files expect the code at `/opt/triage`. Point them at `/opt/triage/app`, then install them:

```bash
cd /opt/triage/app/deploy/ec2
for f in *.service *.timer; do
  sed -e 's#/opt/triage/backend#/opt/triage/app/backend#g' \
      -e 's#/opt/triage/.venv#/opt/triage/app/.venv#g' "$f" | sudo tee /etc/systemd/system/$f >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now triage-api triage-watchdog.timer
for t in northwind helios quill; do sudo systemctl enable --now triage-run@$t.timer; done
```

Run each tenant at 02:00 in its own timezone:

```bash
for pair in northwind:America/New_York helios:Europe/London quill:Asia/Kolkata; do
  t=${pair%%:*}; tz=${pair#*:}
  sudo mkdir -p /etc/systemd/system/triage-run@$t.timer.d
  printf '[Timer]\nOnCalendar=\nOnCalendar=*-*-* 02:00:00 %s\n' "$tz" | \
    sudo tee /etc/systemd/system/triage-run@$t.timer.d/schedule.conf >/dev/null
done
sudo systemctl daemon-reload
```

**Check:**
- `systemctl status triage-api` shows **active (running)**.
- `curl -s localhost:8000/api/health | jq .` returns `"ok": true`, the active model and the last successful run per tenant.
- `systemctl list-timers 'triage-*'` shows 3 run timers and the watchdog, each with a next run time.

---

## 9. HTTPS

There's no need to buy a domain: `sslip.io` turns the IP into a hostname that Caddy can get a Let's Encrypt certificate for. If you own a domain, point an A record at the Elastic IP and use that name instead.

```bash
HOST="$(curl -s https://checkip.amazonaws.com | tr . -).sslip.io"; echo $HOST
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
sudo systemctl reload caddy
```

**Check:** open `https://<HOST>` in a browser, then:
- the padlock shows a valid certificate
- you can log in as `manager@northwind.example`
- the call list loads
- Swagger works at `https://<HOST>/api/docs`

---

## 10. Prove the unattended path works

```bash
sudo systemctl start triage-run@quill            # exactly what the timer does at 02:00
journalctl -u triage-run@quill -o cat | tail -5 | jq -c '{msg, run_id, trace_id}'
triage alert "deployment test"                   # Slack message, or an ERROR line in the log
```

**Check:**
- In the app, **Agent runs** shows a new run with trigger **schedule** and status succeeded.
- The next morning, the overnight runs appear with trigger **schedule** without you doing anything.

---

## Day-2 operations

| Task | Command |
|---|---|
| Deploy a new version | `sudo -u triage git -C /opt/triage/app pull && sudo -u triage bash -c 'cd /opt/triage/app && .venv/bin/pip install -q -r backend/requirements.txt && cd frontend && npm ci && npm run build' && triage migrate && sudo systemctl restart triage-api` |
| API logs | `journalctl -u triage-api -f -o cat \| jq -c .` |
| A run's logs | `journalctl -u triage-run@northwind -o cat \| jq -c .` |
| Re-run or resume | In the app (managers): **Agent runs → Run now / Resume**, or `triage run --resume <run_id>` |
| A user complains | `GET /api/audit/requests?user_name=<name>` → copy the `trace_id` → `journalctl -u triage-api -o cat \| grep <trace_id>` |
| Retrain | `triage train` (the new version becomes active for the next run) |
| Backup | `sudo -u postgres pg_dump triage \| gzip > /opt/triage/backup-$(date +%F).sql.gz` |

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `triage-api` restarts in a loop | Bad value in `/etc/triage/triage.env`. A `# comment` after a value breaks systemd, so put comments on their own line. See `journalctl -u triage-api -n 50` |
| `permission denied to create role` during migrate | The `triage` DB role is missing `CREATEROLE` (step 4) |
| Caddy can't get a certificate | Ports 80 and 443 aren't open in the security group, or the host name doesn't resolve to this IP |
| Run fails at briefs with OpenAI errors | Check the key and quota. The run falls back to template briefs per account, so it still succeeds |
| Out of memory during `train` or `ingest` | Use `t3.large` for the initial load (step 1) |

## Cost and shutdown

- `t3.large` costs about $0.08/hour plus 30 GB gp3 storage, roughly $65/month if left on.
- Stopping the instance keeps the disk and the Elastic IP, but an Elastic IP attached to a stopped instance is billed.
- To tear down: terminate the instance, then release the Elastic IP.
