# Open items

The running list of what's left. When something is finished, move it to **Done** with the date and commit, so this file stays the single place to check status.

_Last updated: 2026-10-18_

## Open

| # | Item | Priority | Notes / next step |
|---|---|---|---|
| 1 | **Deploy to EC2** | High | Server: [EC2_SETUP.md](EC2_SETUP.md); app: [DEPLOYMENT.md](DEPLOYMENT.md) (Docker Compose, 5 steps). The real `python`, `postgres` and `caddy` images and the HTTPS certificate are only exercised on the server |
| 2 | **Prove scheduled runs** | High | After deploying, the overnight runs must show trigger `schedule`. Until then, every run in the DB is `manual` or `backfill`. This is the evidence for the "runs without a human" requirement |
| 3 | **Production secrets on EC2** | High | `TRIAGE_JWT_SECRET` (random), DB password, `OPENAI_API_KEY`; Slack webhook optional (`deploy/docker/.env`, DEPLOYMENT.md step 1) |
| 4 | **HTTPS** | High | Caddy container with an `sslip.io` host name or your own domain (`SITE_HOST`, DEPLOYMENT.md step 4) |
| 5 | **Submission deck (PPT)** | High | Problem reframe → design → model results → agent run → traceability → trade-offs and next steps |
| 6 | **Walkthrough video** | High | Follow [USER_FLOW.md](../USER_FLOW.md): CSM view → account → evidence → draft approval → manager view → runs and recovery |
| 7 | **`git push`** | High | Push the latest commits before sharing the repo |
| 8 | **Data volume vs brief** | Medium | We generate ~1.2M events; the brief says ~200k. Either explain it in the deck (more history for training) or add a ~200k generator option |
| 9 | **Incremental identity matching** | Low | Each run re-checks every account (~1s for 3k accounts). Matching only new records would help at larger scale |
| 10 | **Verify brief reuse with OpenAI on real nightly runs** | Low | Measured with the template writer: 64% of briefs reused the next night. Confirm the rate on EC2 with the OpenAI key (Agent runs page → "(N reused)") |
| 11 | **Real-time path (later)** | Later | Explained as deliberately deferred in BRIEF.md ("Why a nightly run, not real-time") |
| 12 | **Tracing backend (later)** | Later | OTel export is optional and off. Turn on `OTEL_EXPORTER_OTLP_ENDPOINT` plus `deploy/ec2/otel-collector.yaml` if a trace UI is ever needed |

## Done

| Date | Item | How |
|---|---|---|
| 2026-10-18 | Docker deployment | `Dockerfile` (web build + Python runtime, non-root), `deploy/docker/compose.yml` (db, api, scheduler, caddy), built-in `triage scheduler` (tenant-local 02:00 plus watchdog) and an idempotent `triage bootstrap`. Image built and run in a test: API, web app, login, scheduled run and forwarded client IP all verified |
| 2026-10-18 | EC2 server guide | [EC2_SETUP.md](EC2_SETUP.md): `t3.large`, Ubuntu 24.04, 30 GB, Elastic IP, swap, Docker, deploy key |
| 2026-10-18 | Brief step was ~127s per run | Source rows are now sliced once per run rather than once per account (non-LLM part 23s → 5s). Unchanged briefs are reused via a facts fingerprint and re-validated against today's facts; 64% were reused the next night. LLM concurrency 8 → 16 |
| 2026-10-18 | Account page took ~2.5–3s locally | Migration `005_perf.sql` adds indexes. The timeline and evidence queries now look up rows by the account's linked ids instead of joining whole tables (cloud: account 293ms → 18ms, email evidence 329ms → 12ms). Output verified identical |
| 2026-10-18 | Grafana / OTLP removed from the deployment path | OTLP export is optional; logs carry trace_id and user_name |
| 2026-10-18 | `triage.env.example` inline comments | systemd doesn't strip `VALUE  # comment`; comments moved to their own lines |
| earlier | Run recovery, one automatic retry, manager-only re-run | See DECISIONS.md O1–O8 |
| earlier | OpenTelemetry logs, metrics, traces; user_name and trace_id in the API audit | `observability.py`, `api/audit.py` |
