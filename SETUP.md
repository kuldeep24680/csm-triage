# Setup: from clone to a running app

About 15 minutes the first time, most of it waiting for `make demo`. All commands run from the repo root unless a step says otherwise.

## 0. Prerequisites

| Tool | Version | Check | Install (macOS) |
|---|---|---|---|
| Python | 3.12 | `python3.12 --version` | `brew install python@3.12` |
| Node.js | 20.19+ or 22.12+ | `node --version` | `brew install node` |
| Docker Desktop | any recent | `docker compose version` | docker.com, then start it |
| make | any | `make --version` | comes with Xcode command-line tools |

Port **5432** (Postgres), **8000** (API) and **5173** (web) must be free.

## 1. Configure

```bash
cp .env.example .env
```

- The defaults in `.env` already match the Docker database.
- Leave `OPENAI_API_KEY` empty for now. Briefs then come from the built-in template writer, so everything works offline. Step 8 adds the key.

## 2. Install dependencies

```bash
make setup
```

- Creates `.venv/` with the Python packages from `backend/requirements.txt`.
- Runs `npm install` in `frontend/`.

## 3. Start Postgres

```bash
make db
```

Starts Postgres 16 in Docker (user, password and database are all `triage`) and waits until it's healthy.

## 4. Build the demo world (mock data → model → agent runs → logins)

```bash
make demo
```

This runs these steps in order. Each one can also be run on its own:

| Step | Command | What it does | Time |
|---|---|---|---|
| reset | `make reset` | Drops and recreates the schema, including row-level security | seconds |
| generate | `make generate` | Writes ~1.2M synthetic records for 3 companies (Northwind, Helios, Quill) into `data/landing/`, plus the hidden truth in `data/truth/` | ~1 min |
| ingest | `make ingest` | Loads all landing files, de-duplicates, matches records to accounts | ~1.5 min |
| train | `make train` | Builds point-in-time training data, fits and evaluates the model, stores it as active | ~3–4 min |
| backfill | `make backfill` | Replays 8 weekly agent runs (28 Aug → 16 Oct 2026), so "what changed" has history | ~4 min |
| users | `make users` | Creates logins: one manager per company, one per CSM | seconds |

**Expected:** no errors, and the last output lists created users. `data/` is git-ignored. Generation always uses the same seed (7), so the same data comes out every time.

## 5. Start the API (terminal 1)

```bash
make api
```

Check it: open http://localhost:8000/api/health. You should see `"ok": true`, the active model, and the last run per company.

## 6. Start the web app (terminal 2)

```bash
make web
```

Open **http://localhost:5173**. The web app forwards `/api` calls to port 8000.

To serve everything from one port instead, run `cd frontend && npm run build` and restart `make api`. The app is then at http://localhost:8000.

## 7. Sign in

All demo logins use the password **`triage-demo`**.

| Login | Sees |
|---|---|
| `manager@northwind.example` | All 20 Northwind CSMs (the planted stories are here) |
| `manager@helios.example` | Helios (10 CSMs) |
| `manager@quill.example` | Quill (4 CSMs, small tenant) |
| any CSM | Only their own book |

CSM logins follow the pattern `firstname.lastname@<company>.example`. To list them:

```bash
docker compose exec db psql -U triage -d triage -c \
  "select tenant_id, email from users where role='csm' order by 1, 2;"
```

Next, follow [USER_FLOW.md](USER_FLOW.md) to validate the app.

## 8. Turn on OpenAI (optional, recommended before the demo)

1. Put your key in `.env`:
   ```bash
   OPENAI_API_KEY=sk-...
   ```
2. Run one agent run for the small tenant:
   ```bash
   cd backend && ../.venv/bin/python -m triage run --tenant quill
   ```
3. Check the result:
   - **Expected:** status `succeeded`, and `brief_generators` shows `openai:gpt-4o-mini` (or `openai:gpt-4o-mini+template` where a field fell back to the template).
   - `dropped_claims` counts sentences the validator removed.
   - If the API call fails, the run finishes as `partial`: every account still gets a template brief, and the error is recorded in that brief's metadata.
4. In the app: **Agent runs** shows the new run, and a Quill account page shows "Written by openai:gpt-4o-mini".

Cost: about 50 briefs for Quill and about 300 for Northwind per run, so a few cents with gpt-4o-mini.

## 9. Everyday commands

| Task | Command |
|---|---|
| Run the agent once, all tenants (what a scheduler would call) | `make run` |
| Run the agent for one tenant or date | `cd backend && ../.venv/bin/python -m triage run --tenant northwind --as-of 2026-10-16` |
| Run the tests | `make test` (15 tests: parsing, grounding, bands, tenant isolation, idempotency, traceability) |
| See every CLI command | `cd backend && ../.venv/bin/python -m triage --help` |
| Rebuild everything from scratch | `make demo` |
| Stop and delete the database | `docker compose down -v` |

**About dates:**
- The synthetic world ends on 16 Oct 2026. Commands without `--as-of` use that date.
- Running `make run` again for the same date creates another run. The app always shows the latest successful one.

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `make setup`: `python3.12: command not found` | Python 3.12 not installed | `brew install python@3.12` |
| `make db`: port 5432 already allocated | A local Postgres is running | Stop it (`brew services stop postgresql`), or change the port in `docker-compose.yml` and `TRIAGE_DATABASE_URL` |
| `connection refused` on any command | Docker isn't running, or the DB container stopped | Start Docker Desktop, then `make db` |
| `no active model: run python -m triage train first` | `make train` didn't finish | `make train`, then `make backfill` |
| App says "no completed agent run yet" | Backfill hasn't run | `make backfill` |
| Sign-in says email/password don't match | Users not seeded | `make users` |
| Page loads but shows only "Loading…" | API not running | `make api` and check `/api/health` |
| Fonts look plain | Google Fonts blocked (offline) | Cosmetic only |
| An agent run shows `partial` | Some briefs failed and fell back to the template | Open **Agent runs → Steps**; usually a bad or missing OpenAI key |
