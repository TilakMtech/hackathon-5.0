import { useEffect, useMemo, useState } from 'react'
import { Bar, BarChart, Cell, LabelList, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { apiKey, get, pct, post, setApiKey } from './api'

// ---------- shared types ----------
type Addr = { line1: string | null; city: string | null; state: string | null; postcode: string | null }
type Extraction = {
  order_lines: { sku: string; qty: number }[]; commitment_type: string; requested_date: string | null
  delivery_address: Addr; missing_fields: string[]; evidence: Record<string, string>; confidence: number
  suspicious_instructions: boolean; notes: string | null
}
type IntakeResult = {
  extraction: Extraction
  guardrails: { decision: string; missing_fields: string[]; flags: string[]; injection_detected: boolean; risks: string[] }
  address_request: null | { subject: string; body: string; checks: Record<string, boolean | number> }
  truth?: { order_lines: { sku: string; qty: number }[]; commitment_type: string; requested_date: string | null; delivery_address: Addr }
  provider: string; prompt: string
}
type Sample = { email_id: string; client_id: string; format: number; received_ts: string; subject: string; body: string; default_address: string; injection: boolean; has_missing: boolean }
type Candidate = { delivery_id: string; client_id: string; client_name: string; promised: string; risk: number; carrier: string; region: string; causes: string[]; outcome_late: boolean; outcome_days_late: number }
type TriggerStats = { window: string; threshold: number; deliveries: number; alerts_fired: number; late: number; precision: number; recall: number }
type AlertResult = {
  facts: Record<string, string | number>; alert: { subject: string; body: string; channel: string }
  checks: { banned_terms: string[]; dates_not_in_facts: string[]; actions_not_in_facts: string[]; names_not_in_facts: string[]; word_count: number; within_80_words: boolean; has_order_ref: boolean; passed: boolean }
  outcome: { late: boolean; days_late: number; delivered_date: string }
  status: 'ready_for_approval' | 'needs_edit'
  judge?: { factual: number; clarity: number; action: number; tone: number; issues: string[] }
  provider: string; prompt: string
}
type AskResult = {
  question: string
  answer: { answer: string; sql: string | null; figures: { label: string; value: string }[]; answerable: boolean }
  trace: { tool: string; input: { query: string }; output: { row_count?: number; error?: string } }[]
  grounding: null | { numbers_in_answer: number; untraced_numbers: number[]; wrong_entity_numbers: number[]; untraced_figures: number[]; no_query_result: boolean; grounded: boolean }
  status: 'verified' | 'unverified' | 'declined'; repaired: boolean
  provider: string; prompt: string
}
type Gate = { gate: string; value: number | null; op: string; threshold: number; passed: boolean | null; why: string; note?: string }
type EvalReport = {
  run_at: string; provider: string; is_mock: boolean; models: { fast: string; smart: string }; prompts: string[]
  duration_s: number; tokens: { input: number; output: number; cache_read: number }
  metrics: {
    intake: Record<string, number> & { accuracy_by_format: Record<string, number> }
    requests: Record<string, number>; alerts: Record<string, number>; evidence: Record<string, number>
  }
  gates: Gate[]; all_gates_passed: boolean; release_qualifying?: boolean; release_ready?: boolean
  coverage?: { emails: number; alerts: number; questions: number; judge: boolean }
  failures: { intake?: { email_id: string; format?: number; problem: string; decision?: string }[] }
}
type EvalJob = { job_id: string; status: 'running' | 'done' | 'failed'; progress: string; steps: number; error?: string; report?: EvalReport }
export type LlmStatus = { provider: string; is_mock: boolean; models?: { fast: string; smart: string }; error?: string; prompts: string[]; auth_required?: boolean }

const DECISION: Record<string, { label: string; tone: string; note: string }> = {
  auto_accept: { label: 'Auto-accept (proposed)', tone: 'good', note: 'Complete, grounded, confident: proposed for the WMS without manual keying. This slice does not write to a WMS yet.' },
  request_info: { label: 'Ask the client', tone: 'warn', note: 'Address incomplete: confirmation request drafted below.' },
  human_review: { label: 'Human review', tone: 'bad', note: 'A guardrail fired or confidence is low: a person checks it.' },
}

function Err({ msg }: { msg: string | null }) {
  return msg ? <div className="card bad">{msg}</div> : null
}

export function ProviderBadge({ s }: { s: LlmStatus | null }) {
  if (!s) return null
  const label = s.error ? `AI: ${s.provider} (not configured)` : s.is_mock ? 'AI: rule-based baseline (mock)' : `AI: ${s.provider} · ${s.models?.fast} / ${s.models?.smart}`
  const badge = <span className={`badge ${s.error ? 'bad' : s.is_mock ? 'warn' : 'good'}`} title={s.prompts.join('\n')}>{label}</span>
  if (!s.auth_required) return badge
  return <>{badge} <input className="small" type="password" placeholder="API key for AI calls" defaultValue={apiKey()} onChange={(e) => setApiKey(e.target.value)} aria-label="API key for AI calls" /></>
}

function Highlight({ text, quotes }: { text: string; quotes: string[] }) {
  const parts = useMemo(() => {
    const qs = quotes.filter((q) => q && q.length > 1).sort((a, b) => b.length - a.length)
    if (!qs.length) return [text]
    const esc = qs.map((q) => q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    return text.split(new RegExp(`(${esc.join('|')})`, 'gi'))
  }, [text, quotes])
  const set = new Set(quotes.map((q) => q.toLowerCase()))
  return <pre className="email">{parts.map((p, i) => (set.has(p.toLowerCase()) ? <mark key={i}>{p}</mark> : <span key={i}>{p}</span>))}</pre>
}

// ---------- Email intake ----------
export function IntakePage() {
  const [samples, setSamples] = useState<Sample[]>([])
  const [sel, setSel] = useState<Sample | null>(null)
  const [body, setBody] = useState('')
  const [res, setRes] = useState<IntakeResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    get<Sample[]>('/api/intake/samples?limit=42').then((s) => { setSamples(s); setSel(s[0]); setBody(s[0].body) }).catch((e: Error) => setError(e.message))
  }, [])
  const run = () => {
    if (!sel) return
    setBusy(true); setError(null); setRes(null)
    const edited = body !== sel.body
    post<IntakeResult>('/api/intake/extract', edited ? { body, client_id: sel.client_id, received_ts: sel.received_ts, default_address: sel.default_address } : { email_id: sel.email_id })
      .then(setRes).catch((e: Error) => setError(e.message)).finally(() => setBusy(false))
  }
  const x = res?.extraction
  const t = res?.truth
  const d = res ? DECISION[res.guardrails.decision] : null
  const ok = (a: unknown, b: unknown) => String(a ?? '').toLowerCase().replace(/[^a-z0-9]/g, '') === String(b ?? '').toLowerCase().replace(/[^a-z0-9]/g, '')
  return (
    <>
      <p className="lead">
        A third of orders arrive by email in 14 layouts, and 28% of them have an incomplete address, which makes 65% of those orders late.
        The agent reads the email (prompt P1). Code then checks every value against the email before anything reaches the warehouse system, and if the address is incomplete, P2 drafts the question to the client.
      </p>
      <div className="grid2">
        <section className="card">
          <div className="row">
            <select value={sel?.email_id ?? ''} onChange={(e) => { const s = samples.find((x) => x.email_id === e.target.value)!; setSel(s); setBody(s.body); setRes(null) }}>
              {samples.map((s) => <option key={s.email_id} value={s.email_id}>{s.email_id} · layout {s.format} · {s.client_id}{s.has_missing ? ' · missing address' : ''}{s.injection ? ' · injection' : ''}</option>)}
            </select>
            <button className="primary" onClick={run} disabled={busy}>{busy ? 'Reading…' : 'Extract order'}</button>
          </div>
          {sel && <div className="muted small">Received {sel.received_ts.replace('T', ' ')} · address on file (must NOT be used): {sel.default_address}</div>}
          {res && x ? <Highlight text={body} quotes={Object.values(x.evidence)} /> : <textarea className="email" value={body} onChange={(e) => setBody(e.target.value)} rows={16} />}
          {res && <button className="link" onClick={() => setRes(null)}>Edit the email text</button>}
        </section>
        <section className="card">
          <Err msg={error} />
          {!res && !error && <div className="muted">Pick an email (try one marked "injection" or "missing address") and click Extract order.</div>}
          {res && x && d && (
            <>
              <div className={`decision ${d.tone}`}><b>{d.label}</b> · {d.note}</div>
              <table>
                <thead><tr><th>Field</th><th>Extracted</th>{t && <th>Correct?</th>}</tr></thead>
                <tbody>
                  {x.order_lines.map((l, i) => <tr key={i}><td>Line {i + 1}</td><td>{l.sku} × {l.qty}</td>{t && <td className={t.order_lines.some((o) => o.sku === l.sku && o.qty === l.qty) ? 'good' : 'bad'}>{t.order_lines.some((o) => o.sku === l.sku && o.qty === l.qty) ? '✓' : '✗'}</td>}</tr>)}
                  <tr><td>Service</td><td>{x.commitment_type}</td>{t && <td className={x.commitment_type === t.commitment_type ? 'good' : 'bad'}>{x.commitment_type === t.commitment_type ? '✓' : '✗'}</td>}</tr>
                  <tr><td>Requested date</td><td>{x.requested_date ?? '—'}</td>{t && <td className={x.requested_date === t.requested_date ? 'good' : 'bad'}>{x.requested_date === t.requested_date ? '✓' : '✗'}</td>}</tr>
                  {(['line1', 'city', 'state', 'postcode'] as const).map((k) => (
                    <tr key={k}><td>{k === 'line1' ? 'Street' : k === 'postcode' ? 'ZIP' : k[0].toUpperCase() + k.slice(1)}</td>
                      <td className={x.delivery_address[k] ? '' : 'warn'}>{x.delivery_address[k] ?? 'missing'}</td>
                      {t && <td className={ok(x.delivery_address[k], t.delivery_address[k]) ? 'good' : 'bad'}>{ok(x.delivery_address[k], t.delivery_address[k]) ? '✓' : '✗'}</td>}</tr>
                  ))}
                  <tr><td>Confidence</td><td>{pct(x.confidence, 0)}</td>{t && <td />}</tr>
                </tbody>
              </table>
              {(res.guardrails.flags.length > 0 || res.guardrails.injection_detected) && (
                <div className="pad"><b>Guardrails</b>
                  <ul>{res.guardrails.injection_detected && <li className="bad">Email contains instructions aimed at the AI; order sent to human review{x.suspicious_instructions ? ' (the model also flagged it)' : ''}.</li>}
                    {res.guardrails.flags.map((f) => <li key={f} className="bad">{f}</li>)}</ul></div>)}
              {res.guardrails.risks.length > 0 && <div className="pad"><b>Lateness risk</b><ul>{res.guardrails.risks.map((r) => <li key={r} className="warn">{r}</li>)}</ul></div>}
              {res.address_request && (
                <div className="draft"><div className="muted small">Drafted request (P2) · {Object.entries(res.address_request.checks).filter(([k]) => k !== 'word_count').map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v ? '✓' : '✗'}`).join(' · ')}</div>
                  <b>{res.address_request.subject}</b><pre>{res.address_request.body}</pre></div>)}
              <div className="muted small">Highlighted text = the evidence quotes the model cited. {res.provider} · {res.prompt}</div>
            </>
          )}
        </section>
      </div>
    </>
  )
}

// ---------- Delay alerts ----------
const CAUSE_SHORT: Record<string, string> = { drv_address: 'address', drv_cutoff: 'cutoff', drv_stock: 'stock', drv_lane: 'lane' }

export function AlertsPage() {
  const [cands, setCands] = useState<Candidate[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [judge, setJudge] = useState(true)
  const [res, setRes] = useState<AlertResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stats, setStats] = useState<TriggerStats | null>(null)
  useEffect(() => { get<Candidate[]>('/api/alerts/candidates?limit=12').then((c) => { setCands(c); setSel(c[0]?.delivery_id ?? null) }).catch((e: Error) => setError(e.message)) }, [])
  useEffect(() => { get<TriggerStats>('/api/alerts/trigger-stats').then(setStats).catch(() => setStats(null)) }, [])
  const run = () => {
    if (!sel) return
    setBusy(true); setError(null)
    post<AlertResult>('/api/alerts/draft', { delivery_id: sel, judge }).then(setRes).catch((e: Error) => setError(e.message)).finally(() => setBusy(false))
  }
  return (
    <>
      <p className="lead">12 of 34 complaints say "nobody told us it would be late". This is a <b>historical replay</b> on August–September orders: the risk model and lane rule were learned on April–July only, an alert fires when the risk at despatch crosses {stats ? Math.round(stats.threshold * 100) : 50}%, and the agent never sees the real delivery date. Code blocks invented dates, actions, names and liability language; a judge prompt (P5) scores each draft. The remedial action is <b>proposed</b>, not executed, so every draft waits for an operations approver.</p>
      {stats && <div className="tiles small-tiles">
        <div className="tile"><div className="tile-label">Alerts that would have fired</div><div className="tile-value">{stats.alerts_fired.toLocaleString()}</div><div className="tile-sub">of {stats.deliveries.toLocaleString()} at-risk-account deliveries</div></div>
        <div className="tile"><div className="tile-label">Precision</div><div className="tile-value">{Math.round(stats.precision * 100)}%</div><div className="tile-sub">of alerts were really late</div></div>
        <div className="tile"><div className="tile-label">Recall</div><div className="tile-value">{Math.round(stats.recall * 100)}%</div><div className="tile-sub">of late deliveries were caught</div></div>
      </div>}
      <div className="grid2">
        <section className="card">
          <h3>Highest-risk deliveries for accounts threatening to leave</h3>
          <table>
            <thead><tr><th /><th>Delivery</th><th>Client</th><th>Promised</th><th className="r">Risk</th><th>Flags at despatch</th><th>Outcome (hidden from agent)</th></tr></thead>
            <tbody>{cands.map((c) => (
              <tr key={c.delivery_id} className={sel === c.delivery_id ? 'selected' : ''} onClick={() => setSel(c.delivery_id)}>
                <td><input type="radio" checked={sel === c.delivery_id} readOnly /></td><td>{c.delivery_id}</td><td>{c.client_id}</td><td>{c.promised}</td>
                <td className="r">{Math.round(c.risk * 100)}%</td><td>{c.causes.map((k) => CAUSE_SHORT[k]).join(', ')}</td>
                <td>{c.outcome_late ? `late ${c.outcome_days_late}d` : 'on time'}</td></tr>))}</tbody>
          </table>
          <div className="row pad">
            <label className="small"><input type="checkbox" checked={judge} onChange={(e) => setJudge(e.target.checked)} /> Score with judge prompt (P5)</label>
            <button className="primary" onClick={run} disabled={busy || !sel}>{busy ? 'Writing…' : 'Draft alert'}</button>
          </div>
        </section>
        <section className="card">
          <Err msg={error} />
          {!res && !error && <div className="muted">Select a delivery and click Draft alert.</div>}
          {res && (
            <>
              <div className={`decision ${res.status === 'ready_for_approval' ? 'good' : 'bad'}`}><b>{res.status === 'ready_for_approval' ? 'Ready for approval' : 'Needs edit'}</b> · {res.checks.word_count} words · banned terms: {res.checks.banned_terms.length ? res.checks.banned_terms.join(', ') : 'none'} · dates not in facts: {res.checks.dates_not_in_facts.length ? res.checks.dates_not_in_facts.join(', ') : 'none'} · actions not in facts: {res.checks.actions_not_in_facts.length ? res.checks.actions_not_in_facts.join(', ') : 'none'}</div>
              <div className="muted small">Before sending, an approver confirms the proposed action: “{res.facts.action}”. Replayed as of {res.facts.replay_at}; real outcome: {res.outcome.late ? `late by ${res.outcome.days_late} days` : 'on time'}.</div>
              <div className="draft"><b>{res.alert.subject}</b><pre>{res.alert.body}</pre></div>
              {res.judge && (
                <div className="tiles small-tiles">
                  {(['factual', 'clarity', 'action', 'tone'] as const).map((k) => <div key={k} className={`tile ${res.judge![k] >= 4 ? 'good' : 'bad'}`}><div className="tile-label">Judge: {k}</div><div className="tile-value">{res.judge![k]}/5</div></div>)}
                </div>)}
              <details><summary className="small">Facts given to the model</summary><pre className="small">{Object.entries(res.facts).map(([k, v]) => `${k}: ${v}`).join('\n')}</pre></details>
              <div className="muted small">{res.provider} · {res.prompt}</div>
            </>
          )}
        </section>
      </div>
    </>
  )
}

// ---------- Ask the data ----------
const EXAMPLES = ['Which carrier is worst?', 'What was OTD for client C049 in August?', "Why were C190's deliveries late?",
  'How much did we pay in penalties by tier?', 'What share of email orders have an incomplete address?', 'What was the weather in July?']

export function AskPage() {
  const [q, setQ] = useState(EXAMPLES[0])
  const [res, setRes] = useState<AskResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ask = (question = q) => {
    setQ(question); setBusy(true); setError(null)
    post<AskResult>('/api/ask', { question }).then(setRes).catch((e: Error) => setError(e.message)).finally(() => setBusy(false))
  }
  return (
    <>
      <p className="lead">Leadership asks in plain English. The evidence agent (P4) writes read-only SQL, runs it, checks the result and answers. Every number in the answer is then traced back to a query result, and questions the data cannot answer are declined instead of guessed.</p>
      <section className="card">
        <div className="row"><input className="ask" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && ask()} />
          <button className="primary" onClick={() => ask()} disabled={busy}>{busy ? 'Thinking…' : 'Ask'}</button></div>
        <div className="chips">{EXAMPLES.map((e) => <button key={e} className="chip" onClick={() => ask(e)}>{e}</button>)}</div>
      </section>
      <Err msg={error} />
      {res && (
        <section className="card">
          <div className={`decision ${res.status === 'declined' ? 'warn' : res.status === 'verified' ? 'good' : 'bad'}`}>
            <b>{res.status === 'declined' ? 'Declined: not in the data' : res.status === 'verified' ? 'Numbers traced to query rows' : 'Unverified: do not use these numbers'}</b>
            {res.grounding && <> · {res.grounding.numbers_in_answer} numbers · {res.grounding.untraced_numbers.length + res.grounding.untraced_figures.length} untraced · {res.grounding.wrong_entity_numbers.length} on the wrong row{res.grounding.no_query_result ? ' · no query result' : ''}</>}
            {res.repaired && <> · re-asked once after a failed check</>}
          </div>
          <div className="muted small">Traced means each number appears in a result row for the carrier, client, region or tier it is stated for. It is not a proof the answer is right; answer accuracy is measured against gold SQL on the Prompt quality tab.</div>
          <p className="answer">{res.answer.answer}</p>
          {res.answer.figures.length > 0 && <div className="chips">{res.answer.figures.map((f) => <span key={f.label} className="chip static"><b>{f.value}</b> {f.label}</span>)}</div>}
          {res.answer.sql && <pre className="sql">{res.answer.sql}</pre>}
          <div className="muted small">{res.trace.length} query step(s) · {res.provider} · {res.prompt}</div>
        </section>
      )}
    </>
  )
}

// ---------- Prompt quality ----------
const fmtVal = (g: Gate) => (g.value === null ? '—' : g.threshold <= 1 && g.op === '>=' ? pct(g.value, 1) : String(g.value))

export function QualityPage() {
  const [r, setR] = useState<EvalReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [progress, setProgress] = useState<string | null>(null)
  useEffect(() => { get<EvalReport>('/api/evals/latest').then(setR).catch(() => setError('No report yet. Click "Run evals".')) }, [])
  const run = () => {
    setBusy(true); setError(null)
    post<EvalJob>('/api/evals/run?limit=300&alerts=20', {}).then((job) => {
      const poll = () => get<EvalJob>(`/api/evals/jobs/${job.job_id}`).then((j) => {
        setProgress(`${j.progress} (${j.steps} steps)`)
        if (j.status === 'running') { setTimeout(poll, 3000); return }
        if (j.status === 'done' && j.report) setR(j.report)
        if (j.status === 'failed') setError(`Eval run failed: ${j.error}`)
        setBusy(false); setProgress(null)
      }).catch((e: Error) => { setError(e.message); setBusy(false) })
      poll()
    }).catch((e: Error) => { setError(e.message); setBusy(false) })
  }
  // a partial run (e.g. --only evidence) has no intake metrics: render what exists, never crash
  const fmt = r?.metrics.intake?.accuracy_by_format ? Object.entries(r.metrics.intake.accuracy_by_format).map(([k, v]) => ({ layout: k, acc: v })) : []
  const i = r?.metrics.intake && 'emails' in r.metrics.intake ? r.metrics.intake : null
  return (
    <>
      <p className="lead">Prompts are tested like code. A release run covers a golden set of 300 synthetic emails in all 14 layouts (incomplete addresses and 20 injection attempts), 12 unseen injection styles held out from prompt tuning, 20 replayed alerts and 17 leadership questions. Crashes count as failures. In CI the release stage runs this on Claude with <code>--enforce</code> and stops the release unless the run is complete and every gate passes.</p>
      <div className="row"><button className="primary" onClick={run} disabled={busy}>{busy ? 'Running…' : 'Run evals now'}</button>{progress && <span className="muted small">{progress}</span>}{r && <span className="muted small">Last run {r.run_at} · {r.provider} · {r.duration_s}s · tokens in/out/cached {r.tokens.input}/{r.tokens.output}/{r.tokens.cache_read}</span>}</div>
      <Err msg={error} />
      {r && (r.release_qualifying
        ? <div className={`card ${r.release_ready ? 'good' : 'bad'}`}><b>Release-qualifying run: {r.release_ready ? 'release ready' : 'release blocked'}</b> · {r.coverage?.emails} emails, {r.coverage?.alerts} alerts, {r.coverage?.questions} questions, judge on</div>
        : <div className="card warn"><b>Partial or baseline run: a smoke test, not release qualification</b>{r.coverage && <> · {r.coverage.emails} emails, {r.coverage.alerts} alerts, {r.coverage.questions} questions, judge {r.coverage.judge ? 'on' : 'off'}</>}</div>)}
      {r && !i && (
        <section className="card"><h3>Release gates</h3>
          <table><tbody>{r.gates.map((g) => <tr key={g.gate}><td>{g.gate}</td><td className="r">{fmtVal(g)}</td><td className={g.passed === null ? 'muted' : g.passed ? 'good' : 'bad'}>{g.passed === null ? 'not run' : g.passed ? 'pass' : 'fail'}</td></tr>)}</tbody></table>
        </section>)}
      {r && i && (
        <>
          {r.is_mock && <div className="card warn">This run used the <b>rule-based baseline (mock)</b>, not a model. It shows why rules are not enough: the gates catch its mistakes. Set <code>OTD_LLM_PROVIDER=claude_foundry</code> to evaluate the real prompts on the same golden set.</div>}
          <div className="tiles">
            <div className={`tile ${i.fully_correct >= 0.98 ? 'good' : 'bad'}`}><div className="tile-label">Orders fully correct</div><div className="tile-value">{pct(i.fully_correct)}</div><div className="tile-note">{i.emails} emails, 14 layouts</div></div>
            <div className={`tile ${i.unsafe_auto_accepts === 0 ? 'good' : 'bad'}`}><div className="tile-label">Wrong orders auto-accepted</div><div className="tile-value">{i.unsafe_auto_accepts}</div><div className="tile-note">automation rate {pct(i.automation_rate, 0)}</div></div>
            <div className={`tile ${i.missing_field_recall >= 0.99 ? 'good' : 'bad'}`}><div className="tile-label">Incomplete addresses caught</div><div className="tile-value">{pct(i.missing_field_recall)}</div><div className="tile-note">invented values: {i.invented_address_values}</div></div>
            <div className={`tile ${i.injection_obeyed === 0 ? 'good' : 'bad'}`}><div className="tile-label">Injection attempts obeyed</div><div className="tile-value">{i.injection_obeyed} / {i.injection_cases}</div><div className="tile-note">{pct(i.injection_sent_to_review, 0)} sent to human review</div></div>
          </div>
          <div className="grid2">
            <section className="card">
              <h3>Release gates {r.all_gates_passed ? '· all passed' : '· not all passed'}</h3>
              <table><thead><tr><th>Gate</th><th className="r">Value</th><th className="r">Threshold</th><th>Result</th></tr></thead>
                <tbody>{r.gates.map((g) => (
                  <tr key={g.gate} title={g.why}><td>{g.gate}<div className="muted small">{g.note ?? g.why}</div></td><td className="r">{fmtVal(g)}</td><td className="r">{g.op} {g.threshold <= 1 && g.op === '>=' ? pct(g.threshold, 0) : g.threshold}</td>
                    <td className={g.passed === null ? 'muted' : g.passed ? 'good' : 'bad'}>{g.passed === null ? 'n/a' : g.passed ? 'pass' : 'fail'}</td></tr>))}</tbody></table>
            </section>
            <section className="card">
              <h3>Orders fully correct, by email layout</h3>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={fmt} margin={{ top: 20, right: 8, left: 0, bottom: 0 }}>
                  <XAxis dataKey="layout" fontSize={12} />
                  <YAxis domain={[0, 1]} tickFormatter={(v: number) => pct(v, 0)} fontSize={12} />
                  <Tooltip formatter={(v: number) => pct(v)} />
                  <ReferenceLine y={0.98} stroke="var(--accent)" strokeDasharray="4 4" />
                  <Bar dataKey="acc" isAnimationActive={false}>
                    {fmt.map((f) => <Cell key={f.layout} fill={f.acc >= 0.98 ? 'var(--grey)' : 'var(--bad)'} />)}
                    <LabelList dataKey="acc" position="top" fontSize={11} fill="var(--ink)" formatter={(v: number) => `${Math.round(v * 100)}`} />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
              <h3>First failures (debug these prompts first)</h3>
              <table><tbody>{(r.failures.intake ?? []).slice(0, 8).map((f) => <tr key={f.email_id}><td>{f.email_id}</td><td>layout {f.format}</td><td>{f.problem}</td><td className="muted">{f.decision}</td></tr>)}</tbody></table>
              <div className="muted small pad">Prompts under test: {r.prompts.join(', ')}</div>
            </section>
          </div>
        </>
      )}
    </>
  )
}
