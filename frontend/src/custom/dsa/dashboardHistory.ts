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
}

const ISO_DAY = /(20\d{2})[-/](\d{2})[-/](\d{2})/
const CN_DAY = /(20\d{2})年(\d{1,2})月(\d{1,2})日/

function text(value: unknown, fallback = ''): string {
  if (value == null || value === '') return fallback
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  return fallback
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
  const match = cards.find(card => !isMarketReview(card) && watchKey(card.stockCode) === key && card.stockName)
  return match?.stockName || code
}
