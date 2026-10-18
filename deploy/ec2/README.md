# Single-EC2 deployment (systemd)

> **Alternative deployment.** The main path is Docker Compose ([project_context/DEPLOYMENT.md](../../project_context/DEPLOYMENT.md)). Use these systemd units only if you deploy without Docker; never run both.

Everything runs on one EC2 instance; scheduling, retries, alerting and recovery live in the app.

| Piece | How |
|---|---|
| API + web app | `triage-api.service` (uvicorn on 127.0.0.1:8000, behind nginx/Caddy for TLS) |
| Nightly run | `triage-run@<tenant>.timer` → `triage-run@<tenant>.service`, one per tenant, in the tenant's timezone |
| One automatic retry | inside the run: a failed run is resumed once from the failed step |
| Overlap protection | `flock` per tenant + a Postgres advisory lock |
| Stuck / missing runs | `triage-watchdog.timer` (06:30 UTC): marks runs with no heartbeat for 15 min as `abandoned`, starts today's run if missing |
| Alerts | Slack webhook from the app (`TRIAGE_ALERT_WEBHOOK_URL`); systemd `OnFailure=` → `triage-alert@` |
| Manual recovery | CS managers: **Agent runs → Resume** (or **Run now**); CLI: `python -m triage run --resume <run_id>` |
| Telemetry | OpenTelemetry in-process: every log line carries trace_id/span context. Export is optional: set `OTEL_EXPORTER_OTLP_ENDPOINT` + run `otel-collector.yaml` only if a tracing backend is wanted (not needed for this deployment) |
| Logs | JSON to journald (`TRIAGE_LOG_FORMAT=json`), each line with trace_id, request_id, tenant, user_name, run_id |

## Install (Ubuntu 22.04+)
```bash
sudo useradd --system --home /opt/triage triage
sudo git clone <repo> /opt/triage && cd /opt/triage && sudo make setup && (cd frontend && npm run build)
sudo mkdir -p /etc/triage && sudo cp deploy/ec2/triage.env.example /etc/triage/triage.env   # edit secrets
(cd backend && sudo -u triage ../.venv/bin/python -m triage migrate)
sudo cp deploy/ec2/*.service deploy/ec2/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now triage-api triage-watchdog.timer
for t in northwind helios quill; do sudo systemctl enable --now triage-run@$t.timer; done
sudo systemctl edit triage-run@northwind.timer   # OnCalendar=*-*-* 02:00:00 America/New_York (see timer file)
```

## Operate
```bash
systemctl list-timers 'triage-*'                 # next scheduled runs
journalctl -u triage-run@quill -o cat | jq .     # a run's logs (JSON)
systemctl start triage-run@quill                 # run now
cd /opt/triage/backend && ../.venv/bin/python -m triage run --resume <run_id>
../.venv/bin/python -m triage alert "test"       # check the webhook
curl -s localhost:8000/api/health | jq .         # last success per tenant, stuck runs
```

## A user complains ("the page was slow / wrong at 10:05")
1. `GET /api/audit/requests?user_name=<name>` (manager) → find the request, copy its `trace_id`.
2. `journalctl -u triage-api -o cat | grep <trace_id>` → every log line for that request (DB timing via `X-Response-Time-ms` / `latency_ms`).
