export interface DsaStatus {
  enabled: boolean
  base_url: string
  reachable: boolean
  detail: string
}

export interface DsaFeature {
  id: string
  label: string
  section: string
  summary: string
  upstream: string
  overlap: string
}

export interface DsaCatalog {
  upstream_repo: string
  upstream_commit: string
  license: string
  markets: { id: string; label: string; examples: string }[]
  features: DsaFeature[]
  bot_commands: { name: string; usage: string; summary: string; aliases: string[] }[]
  env_vars: { name: string; required: boolean; purpose: string }[]
}

export class DsaError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'DsaError'
    this.status = status
  }
}

function messageFrom(data: unknown, fallback: string): string {
  if (!data || typeof data !== 'object') return fallback
  const record = data as Record<string, unknown>
  const detail = record.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object') {
    const nested = detail as Record<string, unknown>
    if (typeof nested.message === 'string') return nested.message
    if (typeof nested.error === 'string') return nested.error
  }
  if (typeof record.message === 'string') return record.message
  return fallback
}

async function parse<T>(res: Response): Promise<T> {
  const contentType = res.headers.get('content-type') ?? ''
  if (!res.ok) {
    let data: unknown = null
    try { data = await res.json() } catch { data = null }
    throw new DsaError(messageFrom(data, `请求失败（${res.status}）`), res.status)
  }
  if (contentType.includes('application/json')) return res.json() as Promise<T>
  return (await res.text()) as T
}

export function fetchDsaStatus(): Promise<DsaStatus> {
  return fetch('/api/dsa/status').then(res => parse<DsaStatus>(res))
}

export function fetchDsaCatalog(): Promise<DsaCatalog> {
  return fetch('/api/dsa/catalog').then(res => parse<DsaCatalog>(res))
}

export interface DsaQuantSkill {
  name: string
  matched: string[]
  returns: number[]
  sample_sizes: number[]
  verdict: string
  note: string
  guidance: string
}

export interface DsaQuantEvidence {
  available: boolean
  meta: {
    source: string
    report: string
    period: string
    universe: number | null
    benchmark_return: string
    updated: string
    as_of?: string
    env_label?: string
    expires_on?: string
    l5_entry_exit_status?: string
    env_label_missing?: boolean
    freshness?: {
      as_of: string
      expires_on: string
      age_days: number | null
      expiry_days: number
      expired: boolean
      reason: string
    }
  }
  caveat: string
  skills: DsaQuantSkill[]
  expired?: boolean
  sample_below_threshold?: boolean
  min_sample_for_guidance?: number
}

export function fetchDsaQuantEvidence(): Promise<DsaQuantEvidence> {
  return fetch('/api/dsa/quant-evidence').then(res => parse<DsaQuantEvidence>(res))
}

/**
 * P-ledger 证据账本审计视图（只读）。
 *
 * `audit_status` 恒 `unaudited` —— 上游 `backtest_results` 尚未通过 TSP 审计，
 * 展示面必须带「未经 TSP 审计」。`win_rate_pct` 为 `null` 时表示样本不足，
 * 前端不得自行算一个百分比顶上。
 */
export interface DsaLedgerAuditRow {
  analysis_history_id: number
  code: string
  outcome: string
  entry_close: number | null
  entry_next_open: number | null
  entry_gap_pct: number | null
  entry_basis: string
  unfillable: string | null
  in_denominator: boolean
  source: string
  source_note: string
  simulated_return_pct: number | null
}

export interface DsaLedgerAudit {
  available: boolean
  summary: {
    audit_status: string
    badge: string
    rows_total: number
    denominator: number
    wins: number
    losses: number
    excluded_from_denominator: number
    unfillable_count: number
    entry_basis: string[]
    win_rate_pct: number | null
    sample_below_threshold: boolean
    min_sample_for_percent: number
    unavailable_reason: string
  }
  rows: DsaLedgerAuditRow[]
  sources: Record<string, string>
  audit_items?: { id: string; title: string; state: string; detail: string }[]
}

export function fetchDsaLedgerAudit(): Promise<DsaLedgerAudit> {
  return fetch('/api/dsa/ledger-audit').then(res => parse<DsaLedgerAudit>(res))
}

export function dsaUpstream<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const normalized = path.replace(/^\//, '')
  return fetch(`/api/dsa/upstream/${normalized}`, { ...init, headers }).then(res => parse<T>(res))
}

export function dsaCommand(text: string): Promise<{ ok: boolean; command: string; title: string; text: string }> {
  return fetch('/api/dsa/bot/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  }).then(res => parse(res))
}

export interface EtfRotationHolding {
  code: string
  name: string
  bucket: string
  weight: number
  score: number | null
}

export interface EtfRotationView {
  signal_date: string
  mode: string
  mode_label: string
  holdings: EtfRotationHolding[]
  last_rebalance: string | null
  next_rebalance: string | null
  disclaimer: string
  score_kind?: 'avg_rank' | 'momentum' | 'none' | string
  score_hint?: string
  position_basis?: 'target' | 'current' | string
  risk_off?: boolean
}

export interface EtfRotationJob {
  ok: boolean
  detail: string
  command: string
  result?: EtfRotationView
}

export function dsaEtfRotation(): Promise<EtfRotationJob> {
  return fetch('/api/dsa/jobs/etf-rotation', { method: 'POST' }).then(res => parse(res))
}

export interface DsaSchedule {
  enabled: boolean
  time: string
  trading_days_only: boolean
  region: string
  watchlist: string
  timezone: string
  extra_times: string[]
  saved?: boolean
  warnings?: string[]
  scheduler: {
    enabled: boolean
    running: boolean
    schedule_times: string[]
    next_run_at: string | null
    last_run_at: string | null
    last_success_at: string | null
    last_error: string | null
    last_skip_reason: string | null
  }
}

export interface DsaScheduleInput {
  enabled: boolean
  time: string
  trading_days_only: boolean
  region: string
  watchlist: string
}

const SHARE_IMAGE_FALLBACK = '分享图暂时无法生成。请确认决策服务已安装 wkhtmltopdf 和 Noto CJK 中文字体；Docker 部署执行 docker compose --profile dsa up --build 后重试。'

export function fetchShareImage(recordId: string): Promise<string> {
  return fetch(`/api/dsa/upstream/history/${encodeURIComponent(recordId)}/share-image`).then(async res => {
    const contentType = res.headers.get('content-type') ?? ''
    if (res.ok && contentType.includes('image/')) {
      const blob = await res.blob()
      if (blob.size === 0) throw new DsaError(SHARE_IMAGE_FALLBACK, res.status)
      return URL.createObjectURL(blob)
    }
    let data: unknown = null
    try { data = await res.json() } catch { data = null }
    throw new DsaError(messageFrom(data, SHARE_IMAGE_FALLBACK), res.status)
  })
}

export function fetchDsaSchedule(): Promise<DsaSchedule> {
  return fetch('/api/dsa/schedule').then(res => parse<DsaSchedule>(res))
}

export function saveDsaSchedule(body: DsaScheduleInput): Promise<DsaSchedule> {
  return fetch('/api/dsa/schedule', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(res => parse<DsaSchedule>(res))
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

// ── 狙击价位线登记 ──────────────────────────────────────────────

/**
 * 登记狙击线（价位线）的入参。字段与后端 `SniperLineRequest`
 * （`backend/app/custom/dsa/routes.py` L28–35）逐一对齐，名不再自创。
 *
 * **这是价位线，不是监控规则引擎**（软提醒 3）：它只存三个价格（理想买入 / 止损 / 目标），
 * 到价时提醒。和「决策预警 alerts/rules」是两套东西，文案上必须分开写。
 */
export interface SniperLineInput {
  symbol: string
  name?: string
  analysis_id?: string
  analyzed_at?: string
  ideal_buy?: number | null
  stop_loss?: number | null
  take_profit?: number | null
}

/**
 * `POST /api/dsa/sniper-lines` 的响应。
 *
 * `replaced === false` **不是错误**：后端在没有有效价位时会保留旧线并如实返回
 * （`sniper_alerts.register` 的两条 `replaced=False` 分支）。UI 必须照实提示，
 * 别把它渲染成「登记成功」。
 */
export interface SniperLineResult {
  symbol: string
  lines: Record<string, unknown>[]
  replaced: boolean
}

/**
 * 这次登记算不算「成功动作」（用于 L4 自增）。任务书 ④ 的判据：
 * `replaced === true` **或** `lines.length > 0`。
 *
 * 单独抽成函数是为了让「什么算成功」只有一处定义 —— 组件与测试用同一个判据。
 */
export function isSniperRegisterSuccess(result: SniperLineResult): boolean {
  return result.replaced === true || result.lines.length > 0
}

/**
 * 登记三价（理想买入 / 止损 / 目标）。
 *
 * **前置条件是 `reachable`**：不可达时调用方应禁用按钮，不要指望这里兜底
 * （enabled ≠ reachable，见 `DsaNavReachableAccessory` 的说明）。
 *
 * 空值处理：三个价格都传 `null` 时后端会返回 `replaced: false` 并保留旧线 ——
 * 这是**预期行为**，不是 bug。所以 UI 侧最好在提交前就拦住「三价全空」。
 */
export function registerSniperLines(body: SniperLineInput): Promise<SniperLineResult> {
  return fetch('/api/dsa/sniper-lines', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(res => parse<SniperLineResult>(res))
}

export function sampleRecords(rows: readonly object[]): Record<string, unknown>[] {
  return rows.map(row => ({ ...row }))
}

export function asRecords(data: unknown): Record<string, unknown>[] {
  if (Array.isArray(data)) return data.filter(isRecord)
  if (!isRecord(data)) return []
  for (const key of ['items', 'rules', 'triggers', 'accounts', 'skills', 'strategies', 'sources', 'results']) {
    const value = data[key]
    if (Array.isArray(value)) return value.filter(isRecord)
  }
  return []
}

export function textOf(value: unknown, fallback = '—'): string {
  if (value == null || value === '') return fallback
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  return fallback
}
