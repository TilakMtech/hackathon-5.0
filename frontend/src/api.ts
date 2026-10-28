export type Monthly = { month: string; otd: number; deliveries: number }
export type Overview = {
  deliveries: number
  period: { from: string; to: string }
  otd: number
  otd_target: number
  otd_benchmark: number
  otd_clean_orders: number
  clean_orders: number
  late_with_driver_share: number
  monthly: Monthly[]
  cost: { sla_penalties_usd: number; redelivery_usd: number }
  complaints: { total: number; by_category: Record<string, number>; from_threatening_accounts: number }
  revenue_at_risk_usd: number
  threatening_accounts: number
  cs_hours_week: Record<string, number>
  data_quality: { check: string; value: number; expected: string; ok: boolean; note?: string }[]
}
export type DriverRow = {
  key: string; label: string; step: string; fix: string; share: number; deliveries: number
  late_rate_with: number; late_rate_without: number; odds_ratio: number; odds_ratio_ci95: [number, number]; p_value: number
  otd_if_fixed: number; penalties_usd: number
}
export type Drivers = {
  otd_today_model: number
  drivers: DriverRow[]
  routing: {
    odds_ratio_per_stop: number; odds_ratio_per_stop_ci95: [number, number]; p_value_stops: number; p_value_km: number
    otd_if_routes_20pct_shorter: number; late_rate_by_stops: { band: string; late_rate: number }[]
  }
  pseudo_r2: number
  n: number
}
export type Bridge = {
  today: number; all_fixed: number; all_flags_removed: number; scope: string; target: number; benchmark: number
  steps: { step: string; label: string; delta: number }[]
}
export type Lane = { carrier: string; region: string; deliveries: number; late_rate: number; late_rate_clean: number; avg_rate_usd: number; bad: boolean }
export type Lanes = {
  lanes: Lane[]
  carriers: { carrier: string; has_api: boolean; event_feed: string; preferred_supplier: boolean; base_rate_usd: number; late_rate: number; late_rate_clean: number }[]
  recommendations: { region: string; avoid: string[]; use: string; use_late_rate_clean: number; deliveries_to_move_6m: number; extra_cost_usd_per_month: number }[]
  rule: { multiple_of_network: number; floor: number; network_clean_late_rate: number }
}
export type Stock = {
  miss_rate: number; clients_affected: number; median_miles_when_stocked: number; median_miles_when_missed: number
  late_rate_when_missed: number
  matrix: { nearest: string[]; fulfilled_from: string[]; counts: number[][] }
  top_clients: { client_id: string; deliveries: number; misses: number; miss_rate: number; add_stock_at: string }[]
}
export type Account = {
  client_id: string; name: string; tier: string; channel: string; revenue_usd: number; status: string
  deliveries: number; otd: number; penalties_usd: number; complaints: number; main_driver: string
  driver_rates: Record<string, number>
}
export type Accounts = { threatening: Account[]; lowest_otd_tier1: Account[] }
export type SimResult = { otd_today: number; otd_expected: number; meets_target: boolean; late_deliveries_avoided_per_month: number }

const base = import.meta.env.VITE_API_BASE ?? ''

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${base}${path}`)
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json() as Promise<T>
}

// API key for the endpoints that call a paid model, when the server requires one (kept for this browser tab only)
export function apiKey(): string {
  try { return sessionStorage.getItem('otd_api_key') ?? '' } catch { return '' }
}
export function setApiKey(k: string): void {
  try { sessionStorage.setItem('otd_api_key', k) } catch { /* storage unavailable: key lasts until reload */ }
}

export async function post<T>(path: string, body: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const k = apiKey()
  if (k) headers['X-API-Key'] = k
  const r = await fetch(`${base}${path}`, { method: 'POST', headers, body: JSON.stringify(body) })
  if (r.status === 401) throw new Error('This server needs an API key for AI calls: enter it next to the AI badge.')
  if (r.status === 429) throw new Error('Rate limit reached for AI calls: try again in a minute.')
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return r.json() as Promise<T>
}

export const pct = (x: number, d = 1) => `${(x * 100).toFixed(d)}%`
export const usd = (x: number) =>
  x >= 1e6 ? `$${(x / 1e6).toFixed(1)}M` : x >= 1e3 ? `$${Math.round(x / 1e3)}K` : `$${Math.round(x)}`
export const num = (x: number) => x.toLocaleString('en-US')

type OrCi = { or: number; ci95: [number, number] }
type SensRun = { rows: number; otd: number; odds_ratios: Record<string, OrCi>; otd_all_flags_removed: number }
export type ModelChecks = {
  holdout: { split: string; train_rows: number; test_rows: number; auc: number; otd_predicted: number; otd_observed: number
    bad_lanes_train: string[]; same_lanes_as_full_data: boolean; calibration: { decile: number; predicted_late: number; observed_late: number; n: number }[] }
  sensitivity: { all_rows: SensRun; without_chronology_exceptions: SensRun; note: string }
  limits: string[]
}
export type Eligibility = {
  drv_address: { affected: number; reachable_by_email_agent: number; needs_feed_validation: number }
  drv_cutoff: Record<string, number>
}
