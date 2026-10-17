import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, fmtDate, fmtMoney, fmtPct, renewalText } from "./api";
import { Band, BandLegend, Drivers, EvidenceDrawer, Sparkline } from "./components";
import { useMe } from "./ctx";

function useLoad<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const reload = useCallback(() => {
    if (!path) return;
    setErr(null);
    api<T>(path).then(setData).catch((e) => setErr(e.message));
  }, [path]);
  useEffect(() => { setData(null); reload(); }, [reload]);
  return { data, err, reload, setData };
}

function RunStamp({ run }: { run: any }) {
  return (
    <div className="run-stamp">
      Scored <b>{fmtDate(run.as_of)}</b> by the {run.trigger === "schedule" ? "scheduled" : run.trigger} run
      <br />
      <span className="muted">model {run.model_version}</span>
    </div>
  );
}

// ------------------------------------------------------------------ call list
function AccountRow({ r, showOwner }: { r: any; showOwner: boolean }) {
  const change = (r.changes || []).find((c: any) => c.kind !== "first_seen");
  const reasons = r.drivers?.risk || [];
  return (
    <Link to={`/accounts/${r.account_id}`} className={`row ${r.band}`}>
      <div className="rank">{r.band === "unknown" ? "" : r.rank_in_book}</div>
      <div>
        <div className="acct-name">{r.name}</div>
        <div className="acct-meta">{r.segment}, {fmtMoney(r.arr)} ARR</div>
        <div className="acct-meta">{renewalText(r.days_to_renewal)}{r.drivers?.renewal?.source === "inferred_stale_crm" ? " (CRM date out of date)" : ""}</div>
        {showOwner && <div className="acct-meta">{r.owner_name}</div>}
      </div>
      <div className="risk">
        <span className="risk-p">{r.band === "unknown" ? "?" : fmtPct(r.p_churn)}</span>
        <Sparkline points={r.history} />
      </div>
      <div className="reasons-col">
        {r.band === "unknown" ? (
          <p className="small" style={{ margin: 0 }}>No product usage and no contact in 90 days. We can't tell how this account is doing: check in before the renewal.</p>
        ) : (
          <ul className="reasons">{reasons.slice(0, 3).map((d: any) => <li key={d.feature}>{d.label}: {d.display}</li>)}</ul>
        )}
        {r.low_risk_high_value && <div className="small muted" style={{ marginTop: 4 }}>Low risk, but a large renewal is close: a check-in protects the revenue.</div>}
        {change && <div className="changed">Since last week: {change.text}</div>}
      </div>
      <div className="draft-state">
        {r.draft_status === "pending" && <span className="pill pending">Draft to review</span>}
        {r.draft_status === "approved" && <span className="pill approved">Email approved</span>}
        {r.draft_status === "rejected" && <span className="pill rejected">Draft rejected</span>}
      </div>
    </Link>
  );
}

export function CallListPage() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const owner = me.user.role === "manager" ? params.get("owner") || "all" : "";
  const { data, err } = useLoad<any>(`/calllist${owner ? `?owner=${owner}` : ""}`);
  const isTeam = owner === "all";
  const csmName = me.csms.find((c) => c.crm_user_id === owner)?.name;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{me.user.role === "csm" ? "Your calls this week" : isTeam ? "Calls this week, whole team" : `${csmName}'s calls this week`}</h1>
          <p className="sub">Ranked by renewal risk, revenue at stake and how close the renewal is. Each account shows why it's here and what changed.</p>
        </div>
        <div style={{ display: "flex", gap: 16, alignItems: "flex-end", flexWrap: "wrap" }}>
          {me.user.role === "manager" && (
            <select className="picker" aria-label="Choose a CSM" value={owner} onChange={(e) => setParams(e.target.value === "all" ? {} : { owner: e.target.value })}>
              <option value="all">Whole team</option>
              {me.csms.map((c) => <option key={c.crm_user_id} value={c.crm_user_id}>{c.name}</option>)}
            </select>
          )}
          {data && <RunStamp run={data.run} />}
        </div>
      </div>
      {err && <p className="error">{err}</p>}
      {!data && !err && <p className="muted">Loading the call list…</p>}
      {data && (
        <>
          <div className="tally">
            <div className="t-call"><strong>{data.counts.call ?? 0}</strong><span>red: call this week</span></div>
            <div className="t-watch"><strong>{data.counts.watch ?? 0}</strong><span>amber: watch</span></div>
            <div className="t-healthy"><strong>{data.counts.healthy ?? 0}</strong><span>green: healthy</span></div>
            <div className="t-unknown"><strong>{data.counts.unknown ?? 0}</strong><span>grey: can't see</span></div>
            <div><strong>{data.accounts_in_book}</strong><span>accounts {isTeam ? "across the team" : "in the book"}</span></div>
            <div><strong>{fmtMoney(data.expected_arr_at_risk)}</strong><span>expected ARR at risk</span></div>
          </div>
          <BandLegend />
          <section className="board">
            <div className="board-head"><h2><i className="swatch call" />Red: call this week</h2><span className="small muted">capacity {me.tenant.csm_weekly_capacity} per CSM</span></div>
            {data.call.length === 0 && <p className="empty">Nothing needs a call this week. The watch list below is the place to look next.</p>}
            {data.call.map((r: any) => <AccountRow key={r.account_id} r={r} showOwner={isTeam} />)}
          </section>
          {data.unknown.length > 0 && (
            <section className="board">
              <div className="board-head"><h2><i className="swatch unknown" />Grey: can't see, renewing within 120 days</h2><span className="small muted">no data is not good news</span></div>
              {data.unknown.map((r: any) => <AccountRow key={r.account_id} r={r} showOwner={isTeam} />)}
            </section>
          )}
          <section className="board">
            <div className="board-head"><h2><i className="swatch watch" />Amber: watch</h2><span className="small muted">elevated risk, not urgent yet</span></div>
            {data.watch.length === 0 && <p className="empty">No accounts on watch.</p>}
            {data.watch.map((r: any) => <AccountRow key={r.account_id} r={r} showOwner={isTeam} />)}
          </section>
        </>
      )}
    </>
  );
}

// -------------------------------------------------------------------- account
export function AccountPage() {
  const { id } = useParams();
  const me = useMe();
  const { data, err, reload } = useLoad<any>(`/accounts/${id}`);
  const [evidence, setEvidence] = useState<string | null>(null);
  const closeEvidence = useCallback(() => setEvidence(null), []);
  if (err) return <p className="error">{err}</p>;
  if (!data) return <p className="muted">Loading account…</p>;
  const { account: a, score: s, brief, draft } = data;
  const facts: Record<string, any> = Object.fromEntries((brief?.facts || []).map((f: any) => [f.id, f]));
  const changes = (s?.changes || []).filter((c: any) => c.kind !== "first_seen");
  const cov = s?.drivers?.coverage;
  return (
    <>
      <Link to="/" className="back">Back to the call list</Link>
      <div className="acct-head">
        <div>
          <h1>{a.name}</h1>
          <p className="sub">
            {a.segment}, {a.industry}. {fmtMoney(a.arr)} ARR, {a.seats} seats. Owner {a.owner_name}.
            <br />
            {s ? <>{renewalText(s.days_to_renewal)} ({fmtDate(s.drivers?.renewal?.date)})</> : "Not scored in this run"}
            {s?.drivers?.renewal?.source === "inferred_stale_crm" && <> · the CRM renewal date had passed, so this is the next contract anniversary</>}
          </p>
        </div>
        {s && (
          <div style={{ textAlign: "right" }}>
            <div className="big-p">{s.band === "unknown" ? "?" : fmtPct(s.p_churn)}</div>
            <div className="small muted">chance of not renewing</div>
            <div style={{ marginTop: 6 }}><Band band={s.band} />{s.rank_in_book && <span className="small muted"> #{s.rank_in_book} in book</span>}</div>
          </div>
        )}
      </div>

      <div className="cols">
        <div>
          {s && (
            <section className="panel">
              <h2>Why</h2>
              <Drivers risk={s.drivers.risk || []} protective={s.drivers.protective || []} onPick={setEvidence} />
              <p className="small muted" style={{ marginBottom: 0, marginTop: 12 }}>Bars show how much each signal moved the estimate up or down compared with a typical account. Click a signal to see the rows it came from.</p>
            </section>
          )}
          <section className="panel">
            <h2>What changed since last week</h2>
            {changes.length === 0 ? <p className="muted small" style={{ margin: 0 }}>No notable changes since the previous run.</p> : (
              <ul className="points">{changes.map((c: any, i: number) => <li key={i}>{c.text}</li>)}</ul>
            )}
            <div style={{ marginTop: 12 }}><Sparkline points={data.history} width={360} height={56} /></div>
          </section>
          {brief && (
            <section className="panel">
              <h2>Prep for the call</h2>
              <p style={{ marginTop: 0 }}>{brief.summary}</p>
              <ol className="points">
                {brief.talking_points.map((tp: any, i: number) => (
                  <li key={i}>{tp.text}{tp.cites.map((c: string) => <span key={c} className="cite" title={facts[c]?.text}>{c}</span>)}</li>
                ))}
              </ol>
              <p className="generator">
                Written by {brief.generator} from the facts below, for the run on {fmtDate(brief.as_of)}.
                {brief.dropped_claims?.length > 0 && <> {brief.dropped_claims.length} claim(s) were removed because the data didn't support them.</>}
              </p>
              <details>
                <summary className="small">Facts the brief was allowed to use</summary>
                <ul className="small">{brief.facts.map((f: any) => <li key={f.id}><b>{f.id}</b> {f.text}</li>)}</ul>
              </details>
            </section>
          )}
          {draft && <DraftPanel draft={draft} onDone={reload} canAct={me.user.role === "manager" || me.user.crm_user_id === a.owner_id} />}
        </div>

        <div>
          {cov && (
            <section className="panel">
              <h2>What we can see</h2>
              <dl className="kv">
                <dt>Product usage</dt><dd>{cov.has_usage ? "Yes, recent weekly usage" : <span className="band unknown">No recent telemetry</span>}</dd>
                <dt>Recent contact</dt><dd>{cov.recent_contact ? "Yes, emails or tickets in 90 days" : "None in 90 days"}</dd>
                <dt>Linked systems</dt>
                <dd>{data.links.map((l: any) => <div key={l.source + l.external_id} className="small">{l.source.replace("_", " ")}: {l.external_id} <span className="muted">({l.method}, {Math.round(l.confidence * 100)}%)</span></div>)}
                  <div className="small muted">{data.email_addresses_linked} email addresses matched</div></dd>
              </dl>
            </section>
          )}
          <section className="panel">
            <h2>Recent activity</h2>
            {data.timeline.length === 0 && <p className="muted small">Nothing in the last 120 days.</p>}
            <ul className="timeline">
              {data.timeline.slice(0, 30).map((e: any, i: number) => (
                <li key={i}>
                  <span className="when">{fmtDate(e.at)}</span>
                  <div>
                    <span className="src">{e.source}</span>{e.title}
                    {e.detail && <div className="tl-detail">{e.who ? `${e.who}: ` : ""}{e.detail}</div>}
                  </div>
                </li>
              ))}
            </ul>
          </section>
          <section className="panel">
            <h2>How this score was made</h2>
            <dl className="kv small">
              <dt>Run</dt><dd>{String(data.run.run_id).slice(0, 8)}, {fmtDate(data.run.as_of)}</dd>
              <dt>Model</dt><dd>{s?.model_version}</dd>
              <dt>Contacts</dt><dd>{data.contacts.map((c: any) => <div key={c.contact_id}>{c.name} {c.is_champion && <b>(champion)</b>} <span className="muted">{c.email}</span></div>)}</dd>
            </dl>
            <details style={{ marginTop: 10 }}>
              <summary className="small">All {data.features?.length} feature values from this run</summary>
              <table className="data" style={{ marginTop: 8 }}>
                <tbody>
                  {(data.features || []).map((f: any) => (
                    <tr key={f.feature}>
                      <td><button className="cite" style={{ border: 0 }} onClick={() => setEvidence(f.feature)}>rows</button> {f.label}</td>
                      <td className="num">{f.value == null ? "no data" : Number(f.value).toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          </section>
        </div>
      </div>
      {evidence && <EvidenceDrawer accountId={a.account_id} feature={evidence} runId={data.run.run_id} onClose={closeEvidence} />}
    </>
  );
}

function DraftPanel({ draft, onDone, canAct }: { draft: any; onDone: () => void; canAct: boolean }) {
  const [subject, setSubject] = useState(draft.final_subject || draft.subject);
  const [body, setBody] = useState(draft.final_body || draft.body);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setSubject(draft.final_subject || draft.subject); setBody(draft.final_body || draft.body); }, [draft]);
  const decide = async (action: "approve" | "reject") => {
    setBusy(true);
    setErr(null);
    try {
      await api(`/drafts/${draft.draft_id}/${action}`, { method: "POST", body: JSON.stringify({ subject, body }) });
      onDone();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const pending = draft.status === "pending";
  // Gmail's compose URL works in any browser without registering a mailto handler.
  // It only opens a draft in the CSM's own Gmail; nothing is sent until they press Send.
  const gmail = `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(draft.to_email || "")}&su=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  return (
    <section className="panel draft">
      <h2>Draft email {!pending && <span className={`pill ${draft.status}`}>{draft.status === "approved" ? "Approved" : "Rejected"}</span>}</h2>
      <p className="note">Triage never emails customers. Review and edit this draft; once you approve it, send it from your own mailbox.</p>
      <label htmlFor="to">To</label>
      <input id="to" value={draft.to_email || "no contact on file"} readOnly />
      <label htmlFor="subj">Subject</label>
      <input id="subj" value={subject} onChange={(e) => setSubject(e.target.value)} readOnly={!pending} />
      <label htmlFor="body">Message</label>
      <textarea id="body" value={body} onChange={(e) => setBody(e.target.value)} readOnly={!pending} />
      {err && <p className="error">{err}</p>}
      <div className="actions">
        {pending && canAct && <>
          <button className="btn" disabled={busy} onClick={() => decide("approve")}>Approve draft</button>
          <button className="btn secondary" disabled={busy} onClick={() => decide("reject")}>Reject draft</button>
        </>}
        {draft.status === "approved" && <a className="btn" href={gmail} target="_blank" rel="noopener noreferrer">Open in Gmail</a>}
        {!pending && <span className="small muted">{draft.status === "approved" ? "Approved" : "Rejected"} by {draft.decided_by} on {fmtDate(draft.decided_at)}</span>}
      </div>
    </section>
  );
}

// ----------------------------------------------------------------------- team
export function TeamPage() {
  const { data, err } = useLoad<any>("/team");
  return (
    <>
      <div className="page-head">
        <div><h1>Team</h1><p className="sub">Each CSM's book this week. Expected ARR at risk is the sum of risk times ARR across the book.</p></div>
        {data && <RunStamp run={data.run} />}
      </div>
      {err && <p className="error">{err}</p>}
      {data && (
        <div className="board table-wrap">
          <table className="data">
            <thead><tr><th>CSM</th><th>Accounts</th><th>Red</th><th>Amber</th><th>Grey</th><th>Expected ARR at risk</th><th>Drafts approved</th><th>Drafts waiting</th></tr></thead>
            <tbody>
              {data.csms.map((c: any) => (
                <tr key={c.owner_id}>
                  <td><Link to={`/?owner=${c.owner_id}`}>{c.name}</Link></td>
                  <td>{c.accounts}</td><td>{c.call}</td><td>{c.watch}</td><td>{c.unknown}</td>
                  <td>{fmtMoney(c.expected_arr_at_risk)}</td><td>{c.drafts_approved}</td><td>{c.drafts_pending}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ----------------------------------------------------------------------- runs
export function RunsPage() {
  const { data, err } = useLoad<any[]>("/runs");
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <div className="page-head"><div><h1>Agent runs</h1>
        <p className="sub">Every run ingests new data, re-scores every account, works out what changed and drafts briefs. No one has to press a button: runs are triggered on a schedule.</p></div></div>
      {err && <p className="error">{err}</p>}
      {data && (
        <div className="board table-wrap">
          <table className="data">
            <thead><tr><th>Scored for</th><th>Trigger</th><th>Status</th><th>Took</th><th>Records ingested</th><th>Accounts</th><th>Red / amber / grey</th><th>Briefs</th><th>Claims removed</th><th></th></tr></thead>
            <tbody>
              {data.map((r) => {
                const st = r.stats || {};
                const secs = (r.steps || []).reduce((a: number, s: any) => a + s.seconds, 0);
                return (
                  <Fragment key={r.run_id}>
                    <tr>
                      <td>{fmtDate(r.as_of)}</td><td>{r.trigger}</td>
                      <td><span className={`pill ${r.status === "succeeded" ? "approved" : r.status === "failed" ? "pending" : "rejected"}`}>{r.status}</span></td>
                      <td>{secs.toFixed(1)}s</td><td>{st.ingest?.inserted ?? 0}</td><td>{st.accounts_scored}</td>
                      <td>{st.bands?.call ?? 0} / {st.bands?.watch ?? 0} / {st.bands?.unknown ?? 0}</td>
                      <td>{st.briefs} <span className="muted small">{Object.keys(st.brief_generators || {}).join(", ")}</span></td>
                      <td>{st.dropped_claims ?? 0}</td>
                      <td><button className="btn secondary" onClick={() => setOpen(open === r.run_id ? null : r.run_id)}>{open === r.run_id ? "Hide" : "Steps"}</button></td>
                    </tr>
                    {open === r.run_id && (
                      <tr key={r.run_id + "-steps"}><td colSpan={10}>
                        {r.error && <pre className="error small" style={{ whiteSpace: "pre-wrap" }}>{r.error}</pre>}
                        <table className="data"><tbody>{(r.steps || []).map((s: any) => (
                          <tr key={s.step}><td>{s.step}</td><td>{s.status}</td><td>{s.seconds}s</td><td>{s.error}</td></tr>))}</tbody></table>
                        <p className="small muted">run {r.run_id}, model {r.model_version}</p>
                      </td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------- model
function Calibration({ bins }: { bins: { predicted: number; observed: number; n: number }[] }) {
  const W = 260, H = 200, P = 28;
  const max = Math.max(0.4, ...bins.map((b) => Math.max(b.predicted, b.observed)));
  const x = (v: number) => P + (v / max) * (W - P - 8);
  const y = (v: number) => H - P - (v / max) * (H - P - 8);
  return (
    <svg width={W} height={H} role="img" aria-label="Calibration: predicted versus observed churn rate on the test period">
      <line x1={x(0)} y1={y(0)} x2={x(max)} y2={y(max)} stroke="var(--rule)" strokeDasharray="4 3" />
      <line x1={P} y1={H - P} x2={W - 8} y2={H - P} stroke="var(--ink-3)" />
      <line x1={P} y1={H - P} x2={P} y2={8} stroke="var(--ink-3)" />
      {bins.map((b, i) => <circle key={i} cx={x(b.predicted)} cy={y(b.observed)} r={4} fill="var(--call)"><title>{`predicted ${fmtPct(b.predicted)}, observed ${fmtPct(b.observed)} (n=${b.n})`}</title></circle>)}
      <text x={W / 2} y={H - 6} fontSize="11" textAnchor="middle" fill="var(--ink-2)">predicted</text>
      <text x={10} y={H / 2} fontSize="11" textAnchor="middle" fill="var(--ink-2)" transform={`rotate(-90 10 ${H / 2})`}>observed</text>
    </svg>
  );
}

export function ModelPage() {
  const { data, err } = useLoad<any>("/model");
  if (err) return <p className="error">{err}</p>;
  if (!data) return <p className="muted">Loading model card…</p>;
  const m = data.metrics;
  const ops = m.operational?.strategies || {};
  const names: Record<string, string> = { model: "Triage model", rules_ryg: "Red/yellow/green rules", no_risk_signal: "ARR + renewal date only", oracle: "Perfect foresight (ceiling)" };
  return (
    <>
      <div className="page-head"><div><h1>Model</h1>
        <p className="sub">{data.model_version}: {data.algorithm.replace("_", " ")}, trained on renewals up to {fmtDate(m.split_dates.validate_from)}, calibrated on the following 6 months, tested on the last 6 months it never saw.</p></div></div>
      <section className="board">
        <div className="board-head"><h2>Does the weekly list catch churn early?</h2><span className="small muted">replay of {m.operational?.weeks_replayed} weeks, {m.operational?.lost_renewals_in_test} lost renewals</span></div>
        <div className="table-wrap"><table className="data">
          <thead><tr><th>How the list is built</th><th>Churners listed 14 to 90 days before renewal</th><th>Churned ARR listed early</th><th>Precision of the list</th><th>Calls per CSM per week</th></tr></thead>
          <tbody>{["model", "rules_ryg", "no_risk_signal", "oracle"].filter((k) => ops[k]).map((k) => (
            <tr key={k}><td>{names[k]}</td><td>{fmtPct(ops[k].churners_caught_early)}</td><td>{fmtPct(ops[k].churned_arr_caught_early)}</td>
              <td>{fmtPct(ops[k].precision_at_capacity)}</td><td>{ops[k].avg_calls_per_csm_week}</td></tr>))}</tbody>
        </table></div>
        <p className="small muted" style={{ padding: "0 18px 14px" }}>Same capacity and the same weighting for revenue and renewal date in every row; only the risk estimate changes. Precision is capped by how few accounts actually churn: even perfect foresight stays well under 100%.</p>
      </section>
      <div className="grid-2" style={{ marginTop: 24 }}>
        <section className="panel">
          <h2>Ranking quality on the test period</h2>
          <table className="data"><thead><tr><th></th><th>ROC AUC</th><th>PR AUC</th><th>Brier</th></tr></thead>
            <tbody>{Object.entries(m.comparison).map(([k, v]: any) => (
              <tr key={k}><td>{k.replace(/_/g, " ")}{k === m.chosen ? " (shipped)" : ""}</td><td>{v.test.roc_auc}</td><td>{v.test.pr_auc}</td><td>{v.test.brier}</td></tr>))}</tbody></table>
          <p className="small muted">Base rate {fmtPct(m.comparison[m.chosen].test.base_rate)} of renewal snapshots. Your organisation: {Object.entries(m.test_by_tenant).map(([t, v]: any) => `ROC AUC ${v.roc_auc} on ${v.n} snapshots`).join("")}.</p>
        </section>
        <section className="panel">
          <h2>Are the percentages honest?</h2>
          <Calibration bins={m.calibration_test} />
          <p className="small muted">Each dot is a tenth of test accounts. On the dashed line, "20%" means one in five of those accounts actually didn't renew.</p>
        </section>
      </div>
      {m.operational?.planted_lost_renewals && (
        <section className="panel" style={{ marginTop: 24 }}>
          <h2>The two renewals we lost</h2>
          <table className="data"><thead><tr><th>Account</th>{Object.keys(names).map((k) => <th key={k}>{names[k]}</th>)}</tr></thead>
            <tbody>{Object.entries(m.operational.planted_lost_renewals).map(([n, v]: any) => (
              <tr key={n}><td>{n}</td>{Object.keys(names).map((k) => <td key={k}>{v[k]?.first_listed ? `first listed ${fmtDate(v[k].first_listed)}, ${v[k].weeks_listed} weeks` : "never listed"}</td>)}</tr>))}</tbody></table>
        </section>
      )}
      {m.coefficients && (
        <section className="panel" style={{ marginTop: 24 }}>
          <h2>What the model weighs</h2>
          <p className="small muted">Standardised coefficients: positive raises risk. "missing:" means the signal was absent.</p>
          <table className="data"><tbody>{m.coefficients.slice(0, 16).map((c: any) => (
            <tr key={c.feature}><td>{data.feature_descriptions[c.feature] || c.feature}</td><td className="num">{c.coef > 0 ? "+" : ""}{c.coef}</td></tr>))}</tbody></table>
        </section>
      )}
    </>
  );
}

// ---------------------------------------------------------------- data quality
function whyUnmatched(ev: any): string {
  if (ev?.reason === "weak_name_match") return `Name "${ev.name}" is only ${Math.round(ev.score)}% similar to the closest CRM account; below the 92% needed to match.`;
  if (ev?.reason === "ambiguous_domain") return `Domain ${ev.domain} belongs to ${ev.candidates?.length} CRM accounts.`;
  if (ev?.reason === "no_match") return ev.name ? `Nothing resembles "${ev.name}".` : "No CRM id, domain or name to match on.";
  return JSON.stringify(ev);
}

export function DataQualityPage() {
  const { data, err } = useLoad<any>("/data-quality");
  const total = useMemo(() => data?.landing.reduce((a: number, r: any) => a + Number(r.records), 0) ?? 0, [data]);
  if (err) return <p className="error">{err}</p>;
  if (!data) return <p className="muted">Loading data quality…</p>;
  return (
    <>
      <div className="page-head"><div><h1>Data quality</h1>
        <p className="sub">What came in, what was dropped and why, and how records were matched to accounts. {total.toLocaleString()} records received so far.</p></div></div>
      <div className="grid-2">
        <section className="panel">
          <h2>Received</h2>
          <div className="table-wrap"><table className="data"><thead><tr><th>Source</th><th>Files</th><th>Records</th><th>Exact duplicates</th><th>Rejected</th><th>Last file</th></tr></thead>
            <tbody>{data.landing.map((r: any) => <tr key={r.source}><td>{r.source}</td><td>{r.files}</td><td>{Number(r.records).toLocaleString()}</td><td>{r.exact_duplicates}</td><td>{r.rejected}</td><td>{fmtDate(r.last_arrival)}</td></tr>)}</tbody></table></div>
        </section>
        <section className="panel">
          <h2>Duplicates and late data</h2>
          <table className="data"><thead><tr><th>Records</th><th>Raw</th><th>After de-duplication</th></tr></thead>
            <tbody>{data.semantic_dedup.map((r: any) => <tr key={r.source}><td>{r.source}</td><td>{r.raw}</td><td>{r.unique_rows}</td></tr>)}</tbody></table>
          <table className="data" style={{ marginTop: 12 }}><thead><tr><th>Source</th><th>Arrived late</th><th>of</th></tr></thead>
            <tbody>{data.late_arrivals.map((r: any) => <tr key={r.source}><td>{r.source}</td><td>{r.late_rows}</td><td>{r.rows}</td></tr>)}</tbody></table>
          <p className="small muted">Late rows are only used by runs that happen after they arrive, never by earlier ones.</p>
        </section>
        <section className="panel">
          <h2>Matching records to accounts</h2>
          <table className="data"><thead><tr><th>Source</th><th>How</th><th>Count</th><th>Avg confidence</th></tr></thead>
            <tbody>{data.identity.map((r: any, i: number) => <tr key={i}><td>{r.source.replace("_", " ")}</td><td>{r.method}</td><td>{r.n}</td><td>{r.avg_conf}</td></tr>)}</tbody></table>
        </section>
        <section className="panel">
          <h2>What we can't see</h2>
          <dl className="kv">
            <dt>No usage data</dt><dd>{data.coverage.no_usage} of {data.coverage.total} active accounts</dd>
            <dt>Can't score</dt><dd>{data.coverage.insufficient} accounts (no usage and no contact in 90 days)</dd>
            <dt>Stale CRM dates</dt><dd>{data.stale_crm_renewal_dates} renewal dates in the CRM had passed and were inferred</dd>
            <dt>Schema versions</dt><dd>{data.product_schema_versions.map((r: any) => `v${r.schema_version}: ${r.rows}`).join(", ")}</dd>
          </dl>
        </section>
      </div>
      <section className="panel" style={{ marginTop: 24 }}>
        <h2>Needs a human: unmatched records</h2>
        <p className="small muted">These were not matched because nothing safe connected them. A wrong match would mix two customers' data, so we don't guess.</p>
        <div className="table-wrap"><table className="data"><thead><tr><th>Source</th><th>Record</th><th>Why</th></tr></thead>
          <tbody>{data.review_queue.map((r: any) => <tr key={r.source + r.external_id}><td>{r.source}</td><td>{r.external_id}</td><td className="small">{whyUnmatched(r.evidence)}</td></tr>)}</tbody></table></div>
      </section>
      <section className="panel" style={{ marginTop: 24 }}>
        <h2>Rejected lines</h2>
        <div className="table-wrap"><table className="data"><thead><tr><th>Source</th><th>File</th><th>Line</th><th>Reason</th></tr></thead>
          <tbody>{data.rejected_samples.map((r: any, i: number) => <tr key={i}><td>{r.source}</td><td>{r.file_name}</td><td>{r.line_no}</td><td className="small">{r.reason}</td></tr>)}</tbody></table></div>
      </section>
    </>
  );
}
