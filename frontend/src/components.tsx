import { useEffect, useState } from "react";
import { api, fmtDate } from "./api";

export function Sparkline({ points, width = 120, height = 28 }: { points: { p: number; as_of: string }[]; width?: number; height?: number }) {
  if (!points || points.length < 2) return <span className="muted small">new this week</span>;
  const max = Math.max(0.3, ...points.map((x) => x.p));
  const step = width / (points.length - 1);
  const xy = points.map((x, i) => [i * step, height - 3 - (x.p / max) * (height - 6)]);
  const d = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const last = xy[xy.length - 1];
  const first = points[0].p;
  const lastP = points[points.length - 1].p;
  const label = `Risk over ${points.length} weeks: ${Math.round(first * 100)}% to ${Math.round(lastP * 100)}%`;
  return (
    <svg width={width} height={height} role="img" aria-label={label}>
      <title>{label}</title>
      <path d={d} fill="none" stroke="var(--navy)" strokeOpacity="0.55" strokeWidth="1.5" />
      <circle cx={last[0]} cy={last[1]} r="3" fill={lastP >= first ? "var(--danger)" : "var(--success)"} />
    </svg>
  );
}

export function Band({ band }: { band: string }) {
  const label: Record<string, string> = { call: "Call this week", watch: "Watch", healthy: "Healthy", unknown: "Can't see" };
  return <span className={`band ${band}`}>{label[band] ?? band}</span>;
}

type Driver = { feature: string; label: string; display: string; impact: number; value: number | null };

export function Drivers({ risk, protective, onPick }: { risk: Driver[]; protective: Driver[]; onPick: (f: string) => void }) {
  const all = [...risk, ...protective];
  const max = Math.max(0.5, ...all.map((d) => Math.abs(d.impact)));
  const row = (d: Driver) => {
    const w = (Math.abs(d.impact) / max) * 50;
    return (
      <div className="driver" key={d.feature}>
        <div>
          <button onClick={() => onPick(d.feature)} title="Show the source rows behind this number">{d.label}</button>
          <span className="muted">: {d.display}</span>
        </div>
        <div className="bar-track" aria-hidden>
          <div className={`bar ${d.impact > 0 ? "up" : "down"} ${d.value == null ? "missing" : ""}`} style={{ width: `${w}%` }} />
        </div>
      </div>
    );
  };
  return (
    <div className="drivers">
      {risk.length === 0 && <p className="muted small">No single factor stands out; risk comes from many small signals.</p>}
      {risk.map(row)}
      {protective.length > 0 && <p className="small muted" style={{ margin: "8px 0 0" }}>Working in their favour</p>}
      {protective.map(row)}
    </div>
  );
}

const COLS: Record<string, string[]> = {
  usage: ["week_start", "active_users", "sessions", "f_automations", "schema_version", "workspace_id", "arrived_at"],
  tickets: ["created_at", "subject", "priority", "category", "solved_at", "csat", "ticket_id", "arrived_at"],
  emails: ["sent_at", "direction", "counterpart", "subject", "snippet", "arrived_at"],
  billing: ["occurred_at", "event_type", "amount_cents", "prev_quantity", "quantity", "event_id", "arrived_at"],
  crm: ["modified_at", "name", "segment", "arr", "seats", "renewal_date", "status", "owner_id", "arrived_at"],
};

function cell(v: any) {
  if (v == null) return "–";
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return fmtDate(v);
  if (typeof v === "string" && v.length > 90) return v.slice(0, 90) + "…";
  return String(v);
}

export function EvidenceDrawer({ accountId, feature, runId, onClose }: { accountId: string; feature: string; runId?: string; onClose: () => void }) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const q = new URLSearchParams({ feature, ...(runId ? { run_id: runId } : {}) });
    api(`/accounts/${accountId}/evidence?${q}`).then(setData).catch((e) => setErr(e.message));
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [accountId, feature, runId, onClose]);
  const late = data ? data.rows.filter((r: any) => !r.visible_to_run).length : 0;
  return (
    <>
      <div className="drawer-back" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label="Evidence">
        <button className="btn secondary drawer-close" onClick={onClose}>Close</button>
        {err && <p className="error">{err}</p>}
        {!data && !err && <p className="muted">Loading source rows…</p>}
        {data && (
          <>
            <h2>{data.label}</h2>
            <p className="sub">
              Value used by the run on {fmtDate(data.run.as_of)}: <b>{data.value == null ? "no data" : Number(data.value).toFixed(2)}</b>.
              Source: {data.group}, {data.window}.
            </p>
            <p className="small muted">
              Run {String(data.run.run_id).slice(0, 8)} · model {data.run.model_version}
              {late > 0 && <> · {late} hatched row{late > 1 ? "s" : ""} arrived after the run and were not used</>}
            </p>
            <div className="table-wrap">
              <table className="data">
                <thead><tr>{COLS[data.group].map((c) => <th key={c}>{c.replace(/_/g, " ")}</th>)}</tr></thead>
                <tbody>
                  {data.rows.map((r: any, i: number) => (
                    <tr key={i} className={r.visible_to_run ? "" : "late"}>
                      {COLS[data.group].map((c) => <td key={c}>{cell(r[c])}</td>)}
                    </tr>
                  ))}
                  {data.rows.length === 0 && <tr><td colSpan={9} className="muted">No source rows in this window. That absence is itself what the feature measured.</td></tr>}
                </tbody>
              </table>
            </div>
          </>
        )}
      </aside>
    </>
  );
}
