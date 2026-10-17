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
