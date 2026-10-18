"""Fast unit tests: no database needed."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from triage.agent import grounding
from triage.features.build import next_anniversary, urgency
from triage.ingest import normalize as N
from triage.ingest.identity import norm_name
from triage.model.policy import assign_bands, priority

CTX = {"tz": ZoneInfo("America/New_York"), "own_domain": "northwind.example"}


def test_product_v1_and_v2_land_on_the_same_week():
    """v1 is naive local time, v2 is UTC: both must map to the same local Monday."""
    v1 = N.product({"workspace": "ws_1", "period_start": "2025-01-06T00:00:00", "wau": 3, "sess": 9,
                    "events": {}}, CTX)[0][1]
    v2 = N.product({"schema_version": 2, "workspace_id": "ws_1", "period_start": "2025-01-06T05:00:00Z",
                    "active_users": 3, "sessions": 9, "features": {}}, CTX)[0][1]
    assert v1["week_start"] == v2["week_start"] == date(2025, 1, 6)
    # a naive UTC conversion would have put the v2 record on Sunday 5 Jan
    assert v2["week_start"].weekday() == 0


def test_gmail_offsets_normalised_to_utc():
    rec = {"message_id": "<a@x>", "headers": {"From": "jane@acme.com", "To": "sam@northwind.example",
                                              "Date": "Mon, 06 Jan 2025 10:00:00 +0530", "Subject": "hi"}}
    row = N.gmail(rec, CTX)[0][1]
    assert row["sent_at"] == datetime(2025, 1, 6, 4, 30, tzinfo=timezone.utc)
    assert row["direction"] == "inbound" and row["counterpart"] == "jane@acme.com"


def test_zendesk_priority_schema_drift():
    old = N.zendesk({"type": "ticket", "id": 1, "priority": 1, "created_at": "2024-01-01T00:00:00Z"}, CTX)[0][1]
    new = N.zendesk({"type": "ticket", "id": 2, "priority": "Urgent", "created_at": "2025-01-01T00:00:00Z"}, CTX)[0][1]
    assert old["priority"] == new["priority"] == "urgent"


def test_salesforce_renamed_arr_field():
    a = N.salesforce({"sobject": "Account", "Id": "001x", "Name": "Acme", "ACV__c": 1000.0,
                      "Website": "https://www.acme.com/", "LastModifiedDate": "2024-01-01T10:00:00.000+0000"}, CTX)
    b = N.salesforce({"sobject": "Account", "Id": "001x", "Name": "Acme", "ARR__c": 1000.0,
                      "Website": "acme.com", "LastModifiedDate": "2025-01-01T10:00:00.000+0000"}, CTX)
    assert a[0][1]["arr"] == b[0][1]["arr"] == 1000.0
    assert a[0][1]["domain"] == b[0][1]["domain"] == "acme.com"


def test_reject_missing_fields():
    try:
        N.stripe({"id": "evt_1", "type": "invoice.paid", "created": 1, "data": {"object": {}}}, CTX)
    except N.Reject:
        return
    raise AssertionError("expected Reject")


def test_name_normalisation():
    assert norm_name("BRIGHTLINE LOGISTICS, Inc.") == norm_name("Brightline Logistics (Billing)") == "brightline logistics"


def test_anniversary_and_urgency():
    assert next_anniversary(date(2023, 8, 15), date(2026, 8, 1)) == date(2026, 8, 15)
    assert next_anniversary(date(2023, 8, 15), date(2026, 8, 16)) == date(2027, 8, 15)
    assert urgency(60) == 1.0 and urgency(400) == 0.1


def test_bands_respect_capacity_and_unknown():
    book = [dict(p_churn=p, priority=priority(p, 10000, 60), insufficient_signal=False) for p in np.linspace(0.05, 0.6, 20)]
    book.append(dict(p_churn=0.9, priority=99, insufficient_signal=True))
    assign_bands(book, capacity=5, watch_min_p=0.2)
    assert sum(r["band"] == "call" for r in book) == 5
    assert book[-1]["band"] == "unknown"
    assert all(r["band"] in ("watch", "healthy") for r in book[:15])


def test_grounding_drops_unsupported_claims_and_internal_language():
    facts = [{"id": "F1", "kind": "account", "text": "Acme, ARR $50,000, renewal on 2026-11-20 (35 days away).", "ref": None},
             {"id": "F2", "kind": "driver", "text": "high/urgent tickets in the last 90 days: 4.", "ref": None}]
    out, dropped = grounding.validate({
        "summary": "Acme has 4 urgent tickets.",
        "talking_points": [{"text": "They raised 4 urgent tickets", "cites": ["F2"]},
                           {"text": "They have 9 open bugs", "cites": ["F2"]},          # unsupported number
                           {"text": "Renewal is close", "cites": []}],                   # no citation
        "email": {"subject": "Quick call?", "body": "Our model flagged you as a churn risk."}}, facts)
    assert [p["text"] for p in out["talking_points"]] == ["They raised 4 urgent tickets"]
    assert out["email"] is None                                   # leaked internal language
    assert {d["reason"] for d in dropped} >= {"number not supported by cited facts", "no valid citation",
                                              "email leaked internal risk language"}


def test_audit_redacts_sensitive_headers():
    from triage.api.audit import redact_headers
    out = redact_headers({"Authorization": "Bearer secret-token", "Cookie": "s=1", "User-Agent": "curl/8"})
    assert out["authorization"].startswith("Bearer <redacted:") and "secret-token" not in out["authorization"]
    assert out["cookie"] == "<redacted>" and out["user-agent"] == "curl/8"


def test_json_logs_carry_trace_and_user_context():
    import json as _json
    import logging
    from triage.observability import _ContextFilter, _JsonFormatter, tracer, user_name_var
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    tok = user_name_var.set("Chloe Murphy")
    with tracer.start_as_current_span("x"):
        _ContextFilter().filter(rec)
    user_name_var.reset(tok)
    out = _json.loads(_JsonFormatter().format(rec))
    assert out["user_name"] == "Chloe Murphy" and len(out["trace_id"]) == 32


def test_run_steps_keep_pipeline_order_on_resume():
    from datetime import date
    from triage.agent.runner import Run
    r = Run("t", date(2026, 1, 1), "manual", prior_steps=[{"step": "persist_scores", "status": "ok"},
                                                          {"step": "briefs", "status": "failed"}])
    assert r.done == {"persist_scores"}
    r._set_step({"step": "ingest", "status": "ok"})
    assert [s["step"] for s in r.steps] == ["ingest", "persist_scores"]


def test_brief_fingerprint_ignores_daily_drift_but_not_new_facts():
    base = [{"id": "F1", "kind": "account", "text": "Acme, ARR $50,000, renewal on 2026-11-20 (35 days away).", "ref": None},
            {"id": "F2", "kind": "score", "text": "Estimated renewal risk 41% (ranked #3).", "ref": None},
            {"id": "F3", "kind": "driver", "text": "high/urgent tickets in the last 90 days: 4.", "ref": None}]
    tomorrow = [dict(base[0], text="Acme, ARR $50,000, renewal on 2026-11-20 (34 days away)."),
                dict(base[1], text="Estimated renewal risk 44% (ranked #2)."), base[2]]
    new_ticket = [base[0], base[1], dict(base[2], text="high/urgent tickets in the last 90 days: 5.")]
    with_change = base + [{"id": "F4", "kind": "change", "text": "1 new high/urgent support ticket(s)",
                           "ref": {"change": "feature:urgent_tickets_90d"}}]
    fp = grounding.fingerprint(base, "Sam Lee", "openai:gpt-4o-mini")
    assert fp == grounding.fingerprint(tomorrow, "Sam Lee", "openai:gpt-4o-mini")
    assert fp != grounding.fingerprint(new_ticket, "Sam Lee", "openai:gpt-4o-mini")
    assert fp != grounding.fingerprint(with_change, "Sam Lee", "openai:gpt-4o-mini")
    assert fp != grounding.fingerprint(base, "Sam Lee", "template-v2")


def test_template_brief_survives_a_day_of_drift():
    """A reused brief is re-validated against tomorrow's facts: the template must not quote day counts or scores."""
    from triage.agent.llm import TemplateWriter
    facts = [{"id": "F1", "kind": "account", "text": "Acme (SMB), ARR $50,000, renewal on 2026-11-20 (35 days away).", "ref": None},
             {"id": "F2", "kind": "score", "text": "Estimated renewal risk 41% (ranked #3 in the CSM's book this week).", "ref": None},
             {"id": "F3", "kind": "driver", "text": "high/urgent tickets in the last 90 days: 4.", "ref": None}]
    out, dropped = grounding.validate(TemplateWriter().write({"name": "Acme"}, "Sam Lee", facts), facts)
    tomorrow = [dict(facts[0], text=facts[0]["text"].replace("35", "34")), dict(facts[1], text="Estimated renewal risk 44%."), facts[2]]
    again, dropped2 = grounding.validate(out, tomorrow)
    assert not dropped and not dropped2 and again["email"] and len(again["talking_points"]) == len(out["talking_points"])


def test_scheduler_fires_at_tenant_local_time():
    from datetime import datetime, timezone
    from triage.scheduler import next_local, plan
    now = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)           # 20:30 in Kolkata
    assert next_local(now, "02:00", "Asia/Kolkata") == datetime(2026, 10, 18, 20, 30, tzinfo=timezone.utc)
    assert next_local(now, "02:00", "America/New_York") == datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)   # EDT
    winter = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    assert next_local(winter, "02:00", "America/New_York") == datetime(2026, 12, 2, 7, 0, tzinfo=timezone.utc)  # EST
    jobs = plan(now, {"quill": "Asia/Kolkata", "northwind": "America/New_York"})
    assert [j.name for j in jobs] == ["run:quill", "run:northwind", "watchdog"]
    assert jobs[0].argv == ("run", "--tenant", "quill", "--trigger", "schedule")


def test_production_refuses_development_secrets():
    from triage.config import Settings, production_problems
    assert production_problems(Settings(env="development")) == []
    bad = production_problems(Settings(env="production", jwt_secret="short",
                                       database_url="postgresql://triage:triage@localhost/triage"))
    assert len(bad) == 2
    good = Settings(env="production", jwt_secret="a" * 64, database_url="postgresql://triage:s3cr3t-value@db:5432/triage")
    assert production_problems(good) == []


def test_login_is_throttled_after_repeated_failures():
    import pytest
    from fastapi import HTTPException
    from triage.api import auth
    auth._failures.clear()
    keys = ["email:x@y.example", "ip:203.0.113.5"]
    for _ in range(auth.LIMITS["email"]):
        auth._record_failure(keys)
    assert auth._throttled(keys)
    with pytest.raises(HTTPException) as e:
        auth.login("x@y.example", "whatever", "203.0.113.5")
    assert e.value.status_code == 429
    auth._failures.clear()
