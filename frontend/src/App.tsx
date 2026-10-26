import { useEffect, useState } from 'react'
import {
  Bar, BarChart, CartesianGrid, Cell, LabelList, Line, LineChart, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import type { Accounts, Bridge, Drivers, Lanes, Overview, SimResult, Stock } from './api'
import { get, num, pct, post, usd } from './api'

type Tab = 'overview' | 'causes' | 'lanes' | 'stock' | 'accounts' | 'whatif'
const TABS: { id: Tab; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'causes', label: 'Root causes' },
  { id: 'lanes', label: 'Carrier lanes' },
  { id: 'stock', label: 'Stock placement' },
  { id: 'accounts', label: 'Accounts at risk' },
  { id: 'whatif', label: 'What-if' },
]

function useApi<T>(path: string) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    get<T>(path).then(setData).catch((e: Error) => setError(e.message))
  }, [path])
  return { data, error }
}

function Loading({ error }: { error: string | null }) {
  return <div className="muted pad">{error ? `Could not load: ${error}` : 'Loading…'}</div>
}

function Tile({ label, value, note, tone }: { label: string; value: string; note?: string; tone?: 'bad' | 'good' }) {
  return (
    <div className={`tile ${tone ?? ''}`}>
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {note && <div className="tile-note">{note}</div>}
    </div>
  )
}

function OverviewPage() {
  const { data: o, error } = useApi<Overview>('/api/overview')
  if (!o) return <Loading error={error} />
  const cs = o.cs_hours_week
  const csRows = [
    { name: 'Complaints', hours: cs.hours_on_delivery_complaints },
    { name: 'Proactive work', hours: cs.hours_on_proactive_client_work },
    { name: 'Keying email orders', hours: cs.hours_keying_email_orders },
  ]
  return (
    <>
      <p className="lead">
        On-time delivery is <b>{pct(o.otd)}</b> and flat every month. Orders with none of the four upstream
        problems are already <b>{pct(o.otd_clean_orders)}</b> on time, close to the {pct(o.otd_benchmark, 0)} benchmark.
      </p>
      <div className="tiles">
        <Tile label="On-time delivery" value={pct(o.otd)} note={`target ${pct(o.otd_target, 0)} · benchmark ${pct(o.otd_benchmark, 0)}`} tone="bad" />
        <Tile label="OTD on clean orders" value={pct(o.otd_clean_orders)} note={`${num(o.clean_orders)} orders with no upstream problem`} tone="good" />
        <Tile label="Late deliveries with a known driver" value={pct(o.late_with_driver_share, 0)} note="address, stock, cutoff or lane" />
        <Tile label="SLA penalties + redelivery" value={usd(o.cost.sla_penalties_usd + o.cost.redelivery_usd)} note={`${o.period.from} to ${o.period.to}`} />
        <Tile label="Revenue threatening to leave" value={usd(o.revenue_at_risk_usd)} note={`${o.threatening_accounts} accounts · ${o.complaints.from_threatening_accounts} of ${o.complaints.total} complaints`} tone="bad" />
      </div>
      <div className="grid2">
        <section className="card">
          <h3>OTD has been flat for six months</h3>
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={o.monthly} margin={{ top: 16, right: 24, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="month" />
              <YAxis domain={[0.6, 1]} tickFormatter={(v: number) => pct(v, 0)} />
              <Tooltip formatter={(v: number) => pct(v)} />
              <ReferenceLine y={o.otd_benchmark} stroke="var(--muted)" strokeDasharray="4 4" label={{ value: 'Benchmark 94%', position: 'insideTopLeft', fill: 'var(--muted)', fontSize: 12 }} />
              <ReferenceLine y={o.otd_target} stroke="var(--accent)" strokeDasharray="4 4" label={{ value: 'Target 85%', position: 'insideTopLeft', fill: 'var(--accent)', fontSize: 12 }} />
              <Line type="monotone" dataKey="otd" stroke="var(--bad)" strokeWidth={2.5} dot isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </section>
        <section className="card">
          <h3>Customer success spends {pct(cs.hours_on_delivery_complaints / cs.hours_total, 0)} of its week on complaints</h3>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={csRows} layout="vertical" margin={{ top: 16, right: 48, left: 24, bottom: 0 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="name" width={140} />
              <Bar dataKey="hours" fill="var(--grey)" isAnimationActive={false}>
                {csRows.map((r) => <Cell key={r.name} fill={r.name === 'Complaints' ? 'var(--bad)' : 'var(--grey)'} />)}
                <LabelList dataKey="hours" position="right" fill="var(--ink)" formatter={(v: number) => `${Math.round(v)} h`} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <div className="muted small">One week, 18 staff, {Math.round(cs.hours_total)} hours in total.</div>
        </section>
      </div>
      <section className="card">
        <h3>Data checked before it was trusted</h3>
        <table>
          <thead><tr><th>Check</th><th className="r">Value</th><th>Expected</th><th>Result</th></tr></thead>
          <tbody>
            {o.data_quality.map((c) => (
              <tr key={c.check}><td>{c.check}</td><td className="r">{num(c.value)}</td><td>{c.expected}</td>
                <td className={c.ok ? 'good' : 'bad'}>{c.ok ? 'pass' : 'review'}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  )
}

const SHORT: Record<string, string> = {
  routing: 'Routing 20% shorter', drv_address: 'Fix addresses', drv_lane: 'Fix carrier lanes',
  drv_cutoff: 'Same-day wave', drv_stock: 'Stock placement', overlap: 'Overlap',
}

function BridgeChart({ b }: { b: Bridge }) {
  let level = b.today
  const rows = [{ name: 'Today', base: 0, value: b.today, kind: 'total', label: pct(b.today) }]
  for (const s of b.steps) {
    const lo = Math.min(level, level + s.delta)
    rows.push({ name: SHORT[s.step] ?? s.label, base: lo, value: Math.max(Math.abs(s.delta), 0.0015), kind: s.delta < 0 ? 'down' : s.delta < 0.001 ? 'zero' : 'up', label: `${s.delta >= 0 ? '+' : '−'}${(Math.abs(s.delta) * 100).toFixed(1)}` })
    level += s.delta
  }
  rows.push({ name: 'All four fixed', base: 0, value: b.all_fixed, kind: 'total', label: pct(b.all_fixed) })
  const color = (k: string) => (k === 'total' ? 'var(--grey)' : k === 'up' ? 'var(--good)' : k === 'down' ? 'var(--bad)' : 'var(--muted)')
  return (
    <ResponsiveContainer width="100%" height={340}>
      <BarChart data={rows} margin={{ top: 24, right: 16, left: 0, bottom: 8 }}>
        <CartesianGrid strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="name" interval={0} fontSize={12} />
        <YAxis domain={[0, 1]} tickFormatter={(v: number) => pct(v, 0)} />
        <ReferenceLine y={b.target} stroke="var(--accent)" strokeDasharray="4 4" label={{ value: 'Target 85%', position: 'insideTopLeft', fill: 'var(--accent)', fontSize: 12 }} />
        <ReferenceLine y={b.benchmark} stroke="var(--muted)" strokeDasharray="4 4" label={{ value: 'Benchmark 94%', position: 'insideTopLeft', fill: 'var(--muted)', fontSize: 12 }} />
        <Bar dataKey="base" stackId="a" fill="transparent" isAnimationActive={false} />
        <Bar dataKey="value" stackId="a" isAnimationActive={false}>
          {rows.map((r) => <Cell key={r.name} fill={color(r.kind)} />)}
          <LabelList dataKey="label" position="top" fontSize={12} fill="var(--ink)" />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

function CausesPage() {
  const { data: d, error } = useApi<Drivers>('/api/drivers')
  const { data: b } = useApi<Bridge>('/api/bridge')
  if (!d || !b) return <Loading error={error} />
  return (
    <>
      <p className="lead">
        Four problems before the truck leaves explain the late deliveries. Making routes 20% shorter moves OTD from{' '}
        {pct(d.otd_today_model)} to {pct(d.routing.otd_if_routes_20pct_shorter)}: nothing.
      </p>
      <section className="card">
        <h3>Fixing the four drivers lifts OTD from {pct(b.today)} to {pct(b.all_fixed)}</h3>
        <BridgeChart b={b} />
        <div className="muted small">Logistic regression on {num(d.n)} deliveries, controlling for commitment type, channel, stops and route km. Each step = that fix alone.</div>
      </section>
      <section className="card">
        <h3>Driver evidence</h3>
        <table>
          <thead><tr><th>Driver</th><th>Process step</th><th className="r">Share of orders</th><th className="r">Late: with</th><th className="r">Late: without</th><th className="r">Odds ratio</th><th className="r">OTD if fixed</th><th className="r">Penalties</th></tr></thead>
          <tbody>
            {d.drivers.map((r) => (
              <tr key={r.key}>
                <td><b>{r.label}</b><div className="muted small">Fix: {r.fix}</div></td>
                <td>{r.step}</td><td className="r">{pct(r.share)}</td>
                <td className="r bad">{pct(r.late_rate_with)}</td><td className="r">{pct(r.late_rate_without)}</td>
                <td className="r">{r.odds_ratio.toFixed(1)}×</td><td className="r good">{pct(r.otd_if_fixed)}</td>
                <td className="r">{usd(r.penalties_usd)}</td>
              </tr>
            ))}
            <tr className="routing">
              <td><b>Route length and stops</b><div className="muted small">What the $680K vendor optimises</div></td>
              <td>9. Delivery</td><td className="r">all</td><td className="r" colSpan={2}>~29% at every stop count</td>
              <td className="r">{d.routing.odds_ratio_per_stop.toFixed(2)}× (p {d.routing.p_value_stops.toFixed(2)})</td>
              <td className="r">{pct(d.routing.otd_if_routes_20pct_shorter)}</td><td className="r">—</td>
            </tr>
          </tbody>
        </table>
      </section>
    </>
  )
}

function heat(rate: number) {
  const a = Math.min(rate / 0.5, 1)
  return `color-mix(in srgb, var(--bad) ${Math.round(a * 85)}%, transparent)`
}

function LanesPage() {
  const { data: l, error } = useApi<Lanes>('/api/lanes')
  if (!l) return <Loading error={error} />
  const regions = [...new Set(l.lanes.map((x) => x.region))].sort()
  const carriers = l.carriers.map((c) => c.carrier)
  const cell = (c: string, r: string) => l.lanes.find((x) => x.carrier === c && x.region === r)
  return (
    <>
      <p className="lead">
        Leadership is partly right: carrier choice matters, but by lane. On orders with no other problem, carriers fail on
        specific regions. A rules table fixes this; a routing engine does not.
      </p>
      <section className="card">
        <h3>Late rate on clean orders, by carrier and destination region</h3>
        <table className="heat">
          <thead><tr><th>Carrier</th>{regions.map((r) => <th key={r} className="c">{r}</th>)}<th>Feed</th><th>Preferred supplier</th></tr></thead>
          <tbody>
            {carriers.map((c) => {
              const info = l.carriers.find((x) => x.carrier === c)!
              return (
                <tr key={c}>
                  <td><b>Carrier {c}</b></td>
                  {regions.map((r) => {
                    const x = cell(c, r)!
                    return <td key={r} className={`c ${x.bad ? 'flag' : ''}`} style={{ background: heat(x.late_rate_clean) }} title={`${num(x.deliveries)} deliveries · $${x.avg_rate_usd}`}>{pct(x.late_rate_clean, 0)}</td>
                  })}
                  <td>{info.event_feed}</td><td>{info.preferred_supplier ? 'Yes' : ''}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <div className="muted small">Outlined cells are flagged: at least {l.rule.multiple_of_network}× the network clean-order late rate ({pct(l.rule.network_clean_late_rate)}) and above {pct(l.rule.floor, 0)}.</div>
      </section>
      <section className="card">
        <h3>Recommended despatch rules</h3>
        <table>
          <thead><tr><th>Destination</th><th>Stop using</th><th>Use instead</th><th className="r">Its clean late rate</th><th className="r">Deliveries moved (6 mo)</th><th className="r">Cost change / month</th></tr></thead>
          <tbody>
            {l.recommendations.map((r) => (
              <tr key={r.region}><td>{r.region}</td><td className="bad">{r.avoid.join(', ') || '—'}</td><td className="good">Carrier {r.use}</td>
                <td className="r">{pct(r.use_late_rate_clean)}</td><td className="r">{num(r.deliveries_to_move_6m)}</td>
                <td className="r">{r.extra_cost_usd_per_month >= 0 ? '+' : '−'}${num(Math.abs(r.extra_cost_usd_per_month))}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  )
}

function StockPage() {
  const { data: s, error } = useApi<Stock>('/api/stock')
  if (!s) return <Loading error={error} />
  return (
    <>
      <p className="lead">
        {pct(s.miss_rate)} of orders ship from a far warehouse because the nearest one had no stock: median{' '}
        {num(s.median_miles_when_missed)} miles instead of {num(s.median_miles_when_stocked)}, and {pct(s.late_rate_when_missed, 0)} arrive late.
        It touches {s.clients_affected} of 280 clients, so it is the placement rule, not a few accounts.
      </p>
      <div className="grid2">
        <section className="card">
          <h3>Where missed orders were shipped from</h3>
          <table className="heat">
            <thead><tr><th>Nearest ↓ / shipped from →</th>{s.matrix.fulfilled_from.map((w) => <th key={w} className="c">{w}</th>)}</tr></thead>
            <tbody>
              {s.matrix.nearest.map((n, i) => (
                <tr key={n}><td><b>{n}</b></td>{s.matrix.counts[i].map((v, j) => <td key={j} className="c">{v ? num(v) : '—'}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="card">
          <h3>Clients with the most stock misses</h3>
          <table>
            <thead><tr><th>Client</th><th className="r">Deliveries</th><th className="r">Misses</th><th className="r">Miss rate</th><th>Add stock at</th></tr></thead>
            <tbody>
              {s.top_clients.slice(0, 10).map((c) => (
                <tr key={c.client_id}><td>{c.client_id}</td><td className="r">{num(c.deliveries)}</td><td className="r">{num(c.misses)}</td><td className="r">{pct(c.miss_rate)}</td><td>{c.add_stock_at}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </>
  )
}

const DRIVER_SHORT: Record<string, string> = { drv_address: 'Address', drv_cutoff: 'Cutoff', drv_stock: 'Stock', drv_lane: 'Lane' }

function AccountsPage() {
  const { data: a, error } = useApi<Accounts>('/api/accounts')
  if (!a) return <Loading error={error} />
  return (
    <>
      <p className="lead">The nine accounts threatening to leave see the same four drivers as everyone else: the fix is the system, not account-by-account escalation.</p>
      <section className="card">
        <h3>Accounts threatening to leave · {usd(a.threatening.reduce((s, x) => s + x.revenue_usd, 0))} annual revenue</h3>
        <table>
          <thead><tr><th>Client</th><th>Tier</th><th>Channel</th><th className="r">Revenue</th><th className="r">OTD</th><th className="r">Complaints</th><th className="r">Penalties</th><th>Biggest driver</th>{Object.keys(DRIVER_SHORT).map((k) => <th key={k} className="r">{DRIVER_SHORT[k]}</th>)}</tr></thead>
          <tbody>
            {a.threatening.map((x) => (
              <tr key={x.client_id}>
                <td><b>{x.client_id}</b></td><td>{x.tier}</td><td>{x.channel}</td><td className="r">{usd(x.revenue_usd)}</td>
                <td className="r bad">{pct(x.otd)}</td><td className="r">{x.complaints}</td><td className="r">{usd(x.penalties_usd)}</td>
                <td>{x.main_driver}</td>
                {Object.keys(DRIVER_SHORT).map((k) => <td key={k} className="r">{pct(x.driver_rates[k], 0)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="muted small">Driver columns: share of that client's deliveries affected.</div>
      </section>
    </>
  )
}

const SLIDERS = [
  { key: 'drv_address', label: 'Addresses repaired at intake', def: 0.8 },
  { key: 'drv_lane', label: 'Bad-lane volume reassigned', def: 1 },
  { key: 'drv_cutoff', label: 'Late same-day orders caught by a 12:00 wave', def: 0.7 },
  { key: 'drv_stock', label: 'Stock misses removed by re-placement', def: 0.6 },
]

function WhatIfPage() {
  const [fixes, setFixes] = useState<Record<string, number>>(Object.fromEntries(SLIDERS.map((s) => [s.key, s.def])))
  const [routeCut, setRouteCut] = useState(0)
  const [res, setRes] = useState<SimResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const t = setTimeout(() => {
      post<SimResult>('/api/simulate', { fixes, route_cut: routeCut }).then(setRes).catch((e: Error) => setError(e.message))
    }, 200)
    return () => clearTimeout(t)
  }, [fixes, routeCut])
  return (
    <>
      <p className="lead">Move the sliders to test any plan, including the vendor's. Every number is the model's expected OTD on the six months of history.</p>
      <div className="grid2">
        <section className="card">
          <h3>Plan</h3>
          {SLIDERS.map((s) => (
            <label key={s.key} className="slider">
              <span>{s.label}</span><b>{pct(fixes[s.key], 0)}</b>
              <input type="range" min={0} max={1} step={0.05} value={fixes[s.key]} onChange={(e) => setFixes({ ...fixes, [s.key]: Number(e.target.value) })} />
            </label>
          ))}
          <label className="slider vendor">
            <span>Routing engine: cut in stops and route km</span><b>{pct(routeCut, 0)}</b>
            <input type="range" min={0} max={0.5} step={0.05} value={routeCut} onChange={(e) => setRouteCut(Number(e.target.value))} />
          </label>
        </section>
        <section className="card center">
          {res ? (
            <>
              <div className="muted">Expected on-time delivery</div>
              <div className={`big ${res.meets_target ? 'good' : 'bad'}`}>{pct(res.otd_expected)}</div>
              <div>from {pct(res.otd_today)} today · target 85%</div>
              <div className="muted pad">{num(res.late_deliveries_avoided_per_month)} fewer late deliveries a month</div>
            </>
          ) : <Loading error={error} />}
        </section>
      </div>
    </>
  )
}

export default function App() {
  const [tab, setTab] = useState<Tab>('overview')
  return (
    <div className="app">
      <header>
        <div>
          <h1>Halcyon OTD Control Tower</h1>
          <div className="muted small">Why deliveries are late, and what fixes it · synthetic hackathon data, Apr–Sep 2026</div>
        </div>
        <nav>
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </nav>
      </header>
      <main>
        {tab === 'overview' && <OverviewPage />}
        {tab === 'causes' && <CausesPage />}
        {tab === 'lanes' && <LanesPage />}
        {tab === 'stock' && <StockPage />}
        {tab === 'accounts' && <AccountsPage />}
        {tab === 'whatif' && <WhatIfPage />}
      </main>
    </div>
  )
}
