/** 决策仪表盘卡片。去重规则与 DSA history 列表一致：同一天的大盘复盘只留最新一条。 */

export interface DashboardCard {
  id: number | null
  stockCode: string
  stockName: string
  advice: string
  score: string
  trend: string
  region: string
  summary: string
  createdAt: string
  read: boolean
  reportType: string
  marketDay: string | null
  /** 分析时股价。没有可用价格时为 null。 */
  price: number | null
  /** 分析时涨跌幅，百分数口径：`1.23` 表示 +1.23%。没有涨跌幅时为 null。 */
  changePctPoints: number | null
}

const ISO_DAY = /(20\d{2})[-/](\d{2})[-/](\d{2})/
const CN_DAY = /(20\d{2})年(\d{1,2})月(\d{1,2})日/

function text(value: unknown, fallback = ''): string {
  if (value == null || value === '') return fallback
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  return fallback
}

function positivePrice(value: unknown): number | null {
  const parsed = finiteNumber(value)
  return parsed != null && parsed > 0 ? parsed : null
}

function finiteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return null
}

const PLACEHOLDER_NAMES = new Set(['N/A', 'NA', 'NONE', 'NULL', '--', '-', 'UNKNOWN', 'TICKER', '未命名'])

/** 名称和代码相同、或只是占位符时，不能当作股票名称展示。 */
export function meaningfulStockName(name: string | null | undefined, code: string): string | null {
  if (!name) return null
  const trimmed = name.trim()
  if (!trimmed) return null
  const upper = trimmed.toUpperCase()
  const codeUpper = code.trim().toUpperCase()
  if (upper === codeUpper || upper === watchKey(code)) return null
  if (PLACEHOLDER_NAMES.has(upper) || trimmed.startsWith('股票')) return null
  return trimmed
}

/** 百分数口径格式化。`1.23` 显示为 `+1.23%`，不把小数制再乘 100。 */
export function formatPercentPoints(value: number): string {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

export function recordId(value: unknown): number | null {
  if (typeof value === 'number' && Number.isInteger(value) && value > 0) return value
  if (typeof value === 'string' && /^\d+$/.test(value)) return Number(value)
  return null
}

export function watchKey(code: string): string {
  const upper = code.trim().toUpperCase()
  if (!upper || upper === 'MARKET') return upper
  const hkPrefix = upper.match(/^HK(\d{1,5})$/)
  if (hkPrefix) return `HK${hkPrefix[1].padStart(5, '0')}`
  const hkSuffix = upper.match(/^(\d{1,5})\.HK$/)
  if (hkSuffix) return `HK${hkSuffix[1].padStart(5, '0')}`
  const dotted = upper.match(/^(\d{6})\.(?:SH|SZ|BJ|SS)$/)
  if (dotted) return dotted[1]
  const prefixed = upper.match(/^(?:SH|SZ|BJ|SS)\.?(\d{6})$/)
  if (prefixed) return prefixed[1]
  return upper
}

export function isMarketReview(card: Pick<DashboardCard, 'stockCode' | 'reportType'>): boolean {
  return card.reportType === 'market_review' || card.stockCode.trim().toUpperCase() === 'MARKET'
}

function formatDay(year: string, month: string, day: string): string {
  return `${year}-${month.padStart(2, '0')}-${day.padStart(2, '0')}`
}

function dayInText(value: string): string | null {
  const iso = ISO_DAY.exec(value)
  if (iso) return formatDay(iso[1], iso[2], iso[3])
  const cn = CN_DAY.exec(value)
  if (cn) return formatDay(cn[1], cn[2], cn[3])
  return null
}

function createdDay(createdAt: string): string | null {
  const match = /(20\d{2})[-/](\d{2})[-/](\d{2})/.exec(createdAt)
  if (!match) return null
  return formatDay(match[1], match[2], match[3])
}

export function marketReviewDay(card: Pick<DashboardCard, 'stockCode' | 'reportType' | 'summary' | 'createdAt'>): string | null {
  if (!isMarketReview(card)) return null
  return dayInText(card.summary) || createdDay(card.createdAt) || 'unknown'
}

function recency(card: DashboardCard): number {
  const parsed = Date.parse(card.createdAt.includes('T') ? card.createdAt : card.createdAt.replace(' ', 'T'))
  return Number.isNaN(parsed) ? 0 : parsed
}

export function toDashboardCard(row: Record<string, unknown>): DashboardCard {
  const card: DashboardCard = {
    id: recordId(row.id),
    stockCode: text(row.stock_code),
    stockName: text(row.stock_name, text(row.stock_code, '未命名')),
    advice: text(row.operation_advice),
    score: text(row.sentiment_score),
    trend: text(row.trend_prediction),
    region: text(row.region),
    summary: text(row.analysis_summary),
    createdAt: text(row.created_at),
    read: text(row.read_at) !== '',
    reportType: text(row.report_type),
    marketDay: null,
    price: positivePrice(row.current_price),
    changePctPoints: finiteNumber(row.change_pct),
  }
  card.marketDay = marketReviewDay(card)
  return card
}

export function dedupeMarketReviewCards(cards: DashboardCard[]): DashboardCard[] {
  const ordered = [...cards].sort((left, right) => {
    const byTime = recency(right) - recency(left)
    if (byTime !== 0) return byTime
    return (right.id ?? 0) - (left.id ?? 0)
  })
  const seen = new Set<string>()
  const kept: DashboardCard[] = []
  for (const card of ordered) {
    if (card.marketDay == null) {
      kept.push(card)
      continue
    }
    if (seen.has(card.marketDay)) continue
    seen.add(card.marketDay)
    kept.push(card)
  }
  return kept
}

export function poolName(code: string, cards: DashboardCard[]): string {
  const key = watchKey(code)
  const match = cards.find(card => {
    if (isMarketReview(card) || watchKey(card.stockCode) !== key) return false
    return meaningfulStockName(card.stockName, card.stockCode) != null
      || meaningfulStockName(card.stockName, code) != null
  })
  if (!match) return code
  return meaningfulStockName(match.stockName, match.stockCode)
    ?? meaningfulStockName(match.stockName, code)
    ?? code
}

/** 把名称表按观察池等价键索引。股票代码 `600105.SH` 与 `600105` 落到同一键。 */
export function namesByWatchKey(names: Record<string, string | null | undefined>): Map<string, string> {
  const map = new Map<string, string>()
  for (const [symbol, name] of Object.entries(names)) {
    const meaningful = meaningfulStockName(name, symbol)
    if (meaningful) map.set(watchKey(symbol), meaningful)
  }
  return map
}

/**
 * 名称优先用标的维表 / 自选里已经解析好的名字，再用分析记录里的名称。
 * 两边都没有可用名称时退回代码本身。
 */
export function resolvePoolName(
  code: string,
  cards: DashboardCard[],
  nameByKey: ReadonlyMap<string, string>,
): string {
  return nameByKey.get(watchKey(code)) || poolName(code, cards)
}

/** 向名称接口查询时带上的 symbol。六位 A 股代码同时试沪深京，只采用命中的结果。 */
export function nameLookupSymbols(code: string): string[] {
  const key = watchKey(code)
  const symbols: string[] = []
  const trimmed = code.trim().toUpperCase()
  if (trimmed.includes('.')) symbols.push(trimmed)
  if (/^\d{6}$/.test(key)) {
    symbols.push(`${key}.SH`, `${key}.SZ`, `${key}.BJ`)
  } else if (/^HK\d{5}$/.test(key)) {
    symbols.push(`${key.slice(2)}.HK`)
  } else if (trimmed) {
    symbols.push(trimmed)
  }
  return [...new Set(symbols)]
}

/** 同一只股票取最新一条带价格或涨跌幅的分析记录。涨跌幅保持百分数口径。 */
export function poolSnapshot(code: string, cards: DashboardCard[]): { price: number | null; changePctPoints: number | null } {
  const key = watchKey(code)
  const matches = cards.filter(card => !isMarketReview(card) && watchKey(card.stockCode) === key)
  const withQuote = matches.find(card => card.price != null || card.changePctPoints != null)
  const card = withQuote ?? matches[0]
  return {
    price: card?.price ?? null,
    changePctPoints: card?.changePctPoints ?? null,
  }
}
