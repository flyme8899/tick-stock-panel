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

export function dsaEtfRotation(): Promise<{ ok: boolean; detail: string; command: string }> {
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
