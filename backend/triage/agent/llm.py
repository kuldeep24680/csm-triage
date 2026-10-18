"""Brief + draft writers.

``OpenAIWriter`` asks the model for JSON grounded in the provided facts.
``TemplateWriter`` is deterministic and needs no network. It is used when no
API key is configured, when the API fails, or when the model's output fails
validation, so a run never blocks on the LLM.
"""
from __future__ import annotations

import json
import logging
import re
import time

from ..config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You help a Customer Success Manager prepare for a call with a customer account.
You are given FACTS (F*) computed from the company's systems and EVENTS (E*) from support tickets,
emails and billing. Use ONLY these. Do not invent numbers, names, dates or events.

Return JSON with exactly these keys:
{
  "summary": "<= 2 sentences: why this account needs attention this week",
  "talking_points": [{"text": "<one concrete point for the call>", "cites": ["F2", "E1"]}],
  "email": {"subject": "...", "body": "..."}
}
Rules:
- 2 to 4 talking points. Each must cite the fact/event ids it relies on.
- Every number you write must appear in the cited facts.
- Refer to the renewal by its date, not by a count of days, and don't quote the risk percentage
  or rank: those change every day, the rest of the brief should stay true until the facts change.
- The email goes to the customer after the CSM reviews it. It must be warm, short (<= 120 words),
  propose a call, reference something specific the customer raised if available, and must NOT
  mention risk, churn, scores, models, flags or internal data. Sign it with the CSM's first name.
"""


def _user_prompt(acct: dict, csm_name: str, facts: list[dict]) -> str:
    lines = [f"Account: {acct['name']}", f"CSM: {csm_name}", "", "FACTS AND EVENTS:"]
    lines += [f"{f['id']}: {f['text']}" for f in facts]
    return "\n".join(lines)


class TemplateWriter:
    name = "template-v2"

    def write(self, acct: dict, csm_name: str, facts: list[dict]) -> dict:
        drivers = [f for f in facts if f["kind"] == "driver"]
        changes = [f for f in facts if f["kind"] == "change"]
        events = [f for f in facts if f["kind"] == "event"]
        acc = next(f for f in facts if f["kind"] == "account")
        reasons = "; ".join(d["text"].rstrip(".") for d in drivers[:2]) or "a mix of weaker signals"
        # No score, rank or day counts in the text: they change daily, and the score is shown next to the brief anyway.
        summary = f"Main reasons this account needs attention: {reasons}."
        points = [{"text": f"Ask about: {d['text']}", "cites": [d["id"]]} for d in drivers[:2]]
        if changes:
            points.append({"text": f"Since last week: {changes[0]['text']}", "cites": [changes[0]["id"]]})
        if events:
            points.append({"text": f"Reference: {events[0]['text']}", "cites": [events[0]["id"]]})
        renewal = re.sub(r" \(\d+ days away\)", "", acc["text"])
        points.append({"text": f"Confirm renewal plans: {renewal}", "cites": [acc["id"]]})
        first = csm_name.split()[0]
        topic = None
        for e in events:
            if e["ref"] and e["ref"].get("table") in ("tickets", "emails"):
                topic = e["ref"].get("subject")
                break
        hook = f" I also wanted to make sure \"{topic}\" is fully sorted on your side." if topic else ""
        body = (f"Hi,\n\nI'd love to set up a short call over the next week or two to hear how things are going "
                f"with the team and make sure you're getting full value ahead of your renewal.{hook}\n\n"
                f"Would any time next week work?\n\nBest,\n{first}")
        return {"summary": summary, "talking_points": points[:4],
                "email": {"subject": f"Time to connect, {acct['name']}?", "body": body}}


class OpenAIWriter:
    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_s, max_retries=2)
        self.name = f"openai:{settings.openai_model}"

    def write(self, acct: dict, csm_name: str, facts: list[dict]) -> dict:
        from ..observability import record, tenant_var, tracer
        with tracer.start_as_current_span("llm.brief", attributes={
                "gen_ai.system": "openai", "gen_ai.request.model": settings.openai_model,
                "triage.account_id": acct["account_id"]}) as span:
            out = self._write(acct, csm_name, facts)
            m = out.get("_meta", {})
            span.set_attribute("gen_ai.usage.input_tokens", m.get("prompt_tokens") or 0)
            span.set_attribute("gen_ai.usage.output_tokens", m.get("completion_tokens") or 0)
            for kind in ("prompt", "completion"):
                if m.get(f"{kind}_tokens"):
                    record("llm_tokens", m[f"{kind}_tokens"], tenant=tenant_var.get(), kind=kind,
                           model=settings.openai_model)
            return out

    def _write(self, acct: dict, csm_name: str, facts: list[dict]) -> dict:
        t0 = time.time()
        resp = self.client.chat.completions.create(
            model=settings.openai_model, temperature=0.2,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": _user_prompt(acct, csm_name, facts)}])
        out = json.loads(resp.choices[0].message.content)
        u = resp.usage
        out["_meta"] = {"latency_ms": int((time.time() - t0) * 1000),
                        "prompt_tokens": getattr(u, "prompt_tokens", None),
                        "completion_tokens": getattr(u, "completion_tokens", None)}
        return out


def get_writer():
    if settings.openai_api_key:
        try:
            return OpenAIWriter()
        except Exception as e:  # pragma: no cover
            log.warning("OpenAI writer unavailable (%s); using template writer", e)
    return TemplateWriter()
