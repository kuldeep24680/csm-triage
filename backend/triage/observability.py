"""OpenTelemetry traces + metrics, and logs that carry the same identifiers.

* Traces: FastAPI requests, every Postgres query, each agent-run step and each
  LLM call. Export is OTLP/HTTP to whatever ``OTEL_EXPORTER_OTLP_ENDPOINT``
  points at (normally an OpenTelemetry Collector on the same EC2 host, which
  forwards to Grafana Cloud / CloudWatch). Without an endpoint, spans are still
  created, so trace ids still appear in logs and in the API audit table.
* Metrics: request rate / errors / latency, run and step durations and
  outcomes, LLM tokens, briefs and dropped claims, audit queue depth.
* Logs: every line carries trace_id, span_id, request_id, tenant, user_name
  and run_id (``TRIAGE_LOG_FORMAT=json`` for one JSON object per line).

To investigate a complaint: find the user's requests by ``user_name`` in
``api_requests`` (or ``/api/audit/requests?user=...``), take the trace_id, and
open that trace / grep the logs for it.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Request / run context, readable from anywhere (logs, spans, audit).
request_id_var = contextvars.ContextVar("request_id", default=None)
tenant_var = contextvars.ContextVar("tenant", default=None)
user_name_var = contextvars.ContextVar("user_name", default=None)
run_id_var = contextvars.ContextVar("run_id", default=None)

_configured = False
tracer = trace.get_tracer("triage")
meter = metrics.get_meter("triage")


class _ContextFilter(logging.Filter):
    def filter(self, record):
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None
        record.span_id = format(ctx.span_id, "016x") if ctx.is_valid else None
        record.request_id = request_id_var.get()
        record.tenant = tenant_var.get()
        record.user_name = user_name_var.get()
        record.run_id = run_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, r):
        out = {"ts": datetime.fromtimestamp(r.created, timezone.utc).isoformat(), "level": r.levelname,
               "logger": r.name, "msg": r.getMessage()}
        for k in ("trace_id", "span_id", "request_id", "tenant", "user_name", "run_id"):
            v = getattr(r, k, None)
            if v:
                out[k] = v
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, r):
        base = super().format(r)
        extra = " ".join(f"{k}={getattr(r, k)}" for k in ("trace_id", "request_id", "tenant", "user_name", "run_id")
                         if getattr(r, k, None))
        return f"{base} [{extra}]" if extra else base


def setup(service_name: str) -> None:
    """Idempotent. Call once per process (API server, CLI/agent run)."""
    global _configured
    if _configured:
        return
    _configured = True
    resource = Resource.create({"service.name": service_name,
                                "deployment.environment": os.getenv("TRIAGE_ENV", "dev")})
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    tp = TracerProvider(resource=resource)
    readers = []
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15_000))
    trace.set_tracer_provider(tp)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        PsycopgInstrumentor().instrument(skip_dep_check=True)
    except Exception:  # instrumentation must never stop the app
        logging.getLogger(__name__).warning("psycopg instrumentation unavailable")

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_ContextFilter())
    if os.getenv("TRIAGE_LOG_FORMAT", "text") == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.getenv("TRIAGE_LOG_LEVEL", "INFO"))
    for noisy in ("httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _init_instruments()


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


# --------------------------------------------------------------- instruments
class M:
    """Metric instruments (created after the provider is set)."""
    api_requests = api_latency = None
    runs = run_duration = step_duration = None
    llm_tokens = briefs = claims_removed = None


def _init_instruments():
    m = metrics.get_meter("triage")
    # user_name is included on request counts so a complaint can be checked per user. That's
    # fine at this scale (tens of users per tenant); at thousands of users drop it from metrics
    # and rely on traces + the audit table, to keep metric cardinality bounded.
    M.api_requests = m.create_counter("triage.api.requests", description="API requests")
    M.api_latency = m.create_histogram("triage.api.latency", unit="ms", description="API latency")
    M.runs = m.create_counter("triage.agent.runs", description="Agent runs by outcome")
    M.run_duration = m.create_histogram("triage.agent.run.duration", unit="s")
    M.step_duration = m.create_histogram("triage.agent.step.duration", unit="s")
    M.llm_tokens = m.create_counter("triage.llm.tokens", description="LLM tokens")
    M.briefs = m.create_counter("triage.briefs", description="Briefs written, by generator")
    M.claims_removed = m.create_counter("triage.briefs.claims_removed")

    def _queue(_options):
        from .api.audit import _q
        yield metrics.Observation(_q.qsize())
    m.create_observable_gauge("triage.audit.queue_size", callbacks=[_queue])


def record(instrument: str, value: float, **attrs) -> None:
    inst = getattr(M, instrument, None)
    if inst is None:
        return
    attrs = {k: v for k, v in attrs.items() if v is not None}
    if hasattr(inst, "add"):
        inst.add(value, attrs)
    else:
        inst.record(value, attrs)
