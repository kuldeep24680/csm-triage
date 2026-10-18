"""In-container scheduler: the Docker replacement for the systemd timers.

* One nightly agent run per tenant at ``run_at`` (default 02:00) in the
  tenant's own timezone, read from the ``tenants`` table each loop, so a new
  tenant or a timezone change needs no config edit.
* The watchdog at ``watchdog_at`` UTC (default 06:30): abandons stuck runs,
  alerts, and starts today's run if one is missing. This also covers a nightly
  run missed while the server was down.
* Every job runs as a child process (``python -m triage ...``): a crash or a
  memory spike in one run can't take the scheduler down, and memory is freed
  when the run ends.
* Jobs run one at a time. Two runs of the same tenant can't overlap anyway
  (Postgres advisory lock), and a single small server shouldn't run several
  heavy jobs at once.
* The run command already retries once and alerts on failure. The scheduler
  adds an alert only for abnormal exits (killed, out of memory, timeout).
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import alerts
from .config import settings

log = logging.getLogger("triage.scheduler")
JOB_TIMEOUT_S = 2 * 3600


@dataclass(frozen=True)
class Job:
    at: datetime          # UTC
    name: str
    argv: tuple[str, ...]


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def next_local(now_utc: datetime, hhmm: str, tz: str) -> datetime:
    """Next occurrence of HH:MM in ``tz`` strictly after ``now_utc``, returned in UTC."""
    z = ZoneInfo(tz)
    h, m = _hm(hhmm)
    local = now_utc.astimezone(z)
    cand = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= local:
        cand = (cand + timedelta(days=1)).replace(hour=h, minute=m)   # re-anchor after DST shifts
    return cand.astimezone(timezone.utc)


def tenant_timezones() -> dict[str, str]:
    from .db import system_tx
    with system_tx() as conn:
        return {r["tenant_id"]: r["timezone"] for r in conn.execute("SELECT tenant_id, timezone FROM tenants")}


def plan(now_utc: datetime, tenants: dict[str, str]) -> list[Job]:
    jobs = [Job(next_local(now_utc, settings.schedule_run_at, tz), f"run:{t}",
                ("run", "--tenant", t, "--trigger", "schedule")) for t, tz in tenants.items()]
    jobs.append(Job(next_local(now_utc, settings.schedule_watchdog_at, "UTC"), "watchdog", ("watchdog",)))
    return sorted(jobs, key=lambda j: j.at)


def execute(job: Job) -> int:
    log.info("job start %s", job.name)
    t0 = time.monotonic()
    try:
        rc = subprocess.run([sys.executable, "-m", "triage", *job.argv], timeout=JOB_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        rc = -999
    secs = round(time.monotonic() - t0, 1)
    log.info("job end %s rc=%s seconds=%s", job.name, rc, secs)
    if rc not in (0, 1):   # 1 = run failed after its retry; the run already sent that alert
        alerts.send(f"Scheduled job {job.name} exited abnormally",
                    {"exit_code": "timeout" if rc == -999 else rc, "seconds": secs})
    return rc


def loop(poll_s: int = 60) -> None:
    log.info("scheduler started: runs at %s tenant-local, watchdog at %s UTC",
             settings.schedule_run_at, settings.schedule_watchdog_at)
    since = datetime.now(timezone.utc)   # jobs due after this instant and up to "now" fire
    last_logged = None
    while True:
        try:
            tenants = tenant_timezones()
        except Exception:
            log.exception("could not read tenants; retrying")
            time.sleep(poll_s)
            continue
        now = datetime.now(timezone.utc)
        jobs = plan(since, tenants)
        due = [j for j in jobs if j.at <= now]
        # Anything that falls due while these jobs run is picked up next loop (window starts at `now`).
        since = now
        for j in due:
            execute(j)
        if not due:
            nxt = jobs[0]
            if (nxt.name, nxt.at) != last_logged:
                log.info("next job %s at %s", nxt.name, nxt.at.isoformat())
                last_logged = (nxt.name, nxt.at)
            time.sleep(max(1.0, min(poll_s, (nxt.at - now).total_seconds())))
