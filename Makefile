PY ?= .venv/bin/python
TRIAGE = cd backend && ../$(PY) -m triage

.PHONY: migrate setup db demo generate ingest train backfill run users api web test reset

setup:            ## create venv + install backend and frontend deps
	python3.12 -m venv .venv && .venv/bin/pip install -q -r backend/requirements.txt
	cd frontend && npm install

db:               ## start Postgres in Docker
	docker compose up -d --wait db

migrate:          ## apply any new SQL migrations (keeps data)
	$(TRIAGE) migrate

reset:            ## drop and recreate the schema
	$(TRIAGE) reset-db --yes

generate:         ## synthetic landing zone, ~1.2M records (about 1 min)
	$(TRIAGE) generate

ingest:
	$(TRIAGE) ingest

train:            ## fit + evaluate + register the model (about 3 min)
	$(TRIAGE) train

backfill:         ## replay 8 weekly agent runs so "what changed" has history
	$(TRIAGE) backfill --weeks 8

run:              ## one agent run for today, all tenants (what the scheduler calls)
	$(TRIAGE) run --trigger schedule

users:
	$(TRIAGE) seed-users

demo: reset generate ingest train backfill users   ## everything, from scratch

api:
	$(TRIAGE) serve --reload --port 8000

web:
	cd frontend && npm run dev

test:
	cd backend && ../$(PY) -m pytest -q
