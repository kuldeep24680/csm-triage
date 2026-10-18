"""Facts in, checked claims out.

The LLM never sees raw tables. It gets a numbered list of facts computed by
the pipeline (F*) and source events (E*), each with a stable reference back
to the database. Its output is then validated:

  * every talking point must cite at least one known fact id;
  * every number in a claim must appear in the facts it cites;
  * the customer email must not leak internal language (risk score, churn,
    "flagged"...) and must not contain numbers absent from the facts.

Claims that fail are dropped and recorded (``dropped_claims``), so nothing
the CSM reads is unsupported by the data.
"""
from __future__ import annotations

import re
from datetime import date

NUM = re.compile(r"(?<![A-Za-z])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
INTERNAL_TERMS = re.compile(r"churn|risk score|at[- ]risk|flagged|our model|probabilit|health score|red account|"
                            r"likelihood|predict", re.I)


def build_facts(acct: dict, score: dict, drivers: dict, changes: list[dict], events: list[dict]) -> list[dict]:
    facts = []

    def add(kind, text, ref=None):
        prefix = "E" if kind == "event" else "F"
        n = sum(1 for f in facts if f["id"].startswith(prefix)) + 1
        facts.append({"id": f"{prefix}{n}", "kind": kind, "text": text, "ref": ref})

    add("account", f"{acct['name']} ({acct['segment']}), ARR ${acct['arr']:,.0f}, "
                   f"renewal on {acct['next_renewal']} ({acct['days_to_renewal']} days away).",
        {"table": "accounts", "account_id": acct["account_id"]})
    add("score", f"Estimated renewal risk {score['p_churn'] * 100:.0f}% "
                 f"(ranked #{score['rank_in_book']} in the CSM's book this week).",
        {"table": "account_scores", "run_id": score["run_id"]})
    for d in drivers.get("risk", []):
        add("driver", f"{d['label']}: {d['display']}.", {"table": "feature_snapshots", "feature": d["feature"]})
    for d in drivers.get("protective", []):
        add("protective", f"(in their favour) {d['label']}: {d['display']}.",
            {"table": "feature_snapshots", "feature": d["feature"]})
    for c in changes:
        add("change", c["text"], {"table": "account_scores", "change": c["kind"]})
    for e in events:
        add("event", e["text"], e["ref"])
    return facts


def _numbers(text: str) -> set[str]:
    out = set()
    for m in NUM.findall(text or ""):
        v = m.replace("$", "").replace(",", "").replace("%", "").lstrip("+-")
        try:
            f = abs(float(v))
        except ValueError:
            continue
        out.add(f"{f:.0f}" if f.is_integer() else f"{f:.1f}")
    return out


def _numbers_ok(text: str, allowed_text: str) -> bool:
    allowed = _numbers(allowed_text)
    # dates are written as numbers too: allow the components of ISO dates in facts
    for d in re.findall(r"\d{4}-\d{2}-\d{2}", allowed_text):
        y, m, dd = d.split("-")
        allowed |= {str(int(y)), str(int(m)), str(int(dd))}
    return _numbers(text) <= allowed


def validate(output: dict, facts: list[dict]) -> tuple[dict, list[dict]]:
    by_id = {f["id"]: f for f in facts}
    all_text = " ".join(f["text"] for f in facts)
    dropped = []
    points = []
    for tp in output.get("talking_points") or []:
        text = (tp or {}).get("text", "").strip()
        cites = [c for c in (tp or {}).get("cites", []) if c in by_id]
        if not text:
            continue
        if not cites:
            dropped.append({"text": text, "reason": "no valid citation"})
            continue
        cited_text = " ".join(by_id[c]["text"] for c in cites)
        if not _numbers_ok(text, cited_text):
            dropped.append({"text": text, "reason": "number not supported by cited facts"})
            continue
        points.append({"text": text, "cites": cites})

    summary = (output.get("summary") or "").strip()
    if not summary or not _numbers_ok(summary, all_text):
        if summary:
            dropped.append({"text": summary, "reason": "summary had unsupported numbers"})
        summary = ""

    email = output.get("email") or {}
    subj, body = (email.get("subject") or "").strip(), (email.get("body") or "").strip()
    email_ok = bool(subj and body)
    if email_ok and INTERNAL_TERMS.search(subj + " " + body):
        dropped.append({"text": body[:200], "reason": "email leaked internal risk language"})
        email_ok = False
    if email_ok and not _numbers_ok(subj + " " + body, all_text):
        dropped.append({"text": body[:200], "reason": "email had unsupported numbers"})
        email_ok = False
    return {"summary": summary, "talking_points": points,
            "email": {"subject": subj, "body": body} if email_ok else None}, dropped


def fmt_date(d) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)[:10]


# Parts of the facts that drift every day without the story changing: the
# day count to renewal, today's score and rank, and week-over-week changes.
_DAYS_AWAY = re.compile(r"\s*\(\d+ days away\)")
_VOLATILE_KINDS = {"score", "change"}


def fingerprint(facts: list[dict], csm: str, generator: str) -> str:
    """Hash of what a brief is *about*. Same fingerprint on the next run ->
    the previous brief can be reused (it is still re-validated against the new facts)."""
    import hashlib
    import json
    stable = [(f["id"], _DAYS_AWAY.sub("", f["text"])) for f in facts if f["kind"] not in _VOLATILE_KINDS]
    # Which things changed since the last run (not their wording) is part of the story:
    # a brief written before a new change can't be reused once the change shows up.
    changed = sorted(f["ref"]["change"] for f in facts if f["kind"] == "change" and f["ref"]["change"] != "risk")
    return hashlib.sha256(json.dumps([generator, csm, stable, changed]).encode()).hexdigest()[:32]
