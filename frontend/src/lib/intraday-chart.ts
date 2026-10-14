import type { MinuteKlineRow } from '@/lib/api'

export type DailySummary = { date: string; open: number | null; high: number; low: number; close: number }

/** 仅汇总实际收到的分钟，不将缺失开盘价或成交额伪装为真实值。 */
export function summarizeMinutes(data: MinuteKlineRow[]): MinuteKlineRow | null {
  if (!data.length) return null
  return {
    datetime: data[0].datetime,
    open: data[0].open,
    high: Math.max(...data.map(row => row.high)),
    low: Math.min(...data.map(row => row.low)),
    close: data[data.length - 1].close,
    volume: data.reduce((total, row) => total + row.volume, 0),
    amount: data.every(row => row.amount != null && Number.isFinite(row.amount))
      ? data.reduce((total, row) => total + row.amount!, 0) : null,
  }
}

/** 从 datetime 串取 HH:MM。契约: 分钟K datetime 已在后端入口统一为北京墙钟, 前端不做时区换算。 */
export function formatMinuteTime(datetime: string): string {
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  return `${match[1]}:${match[2]}`
}

// 与 backend/app/services/amount_guard.py 同一套分钟防护。
// volume 是手。接口返回的价格和 amount 已经按同一复权因子缩放过。
const LOT_SHARES = 100
const LOW_FACTOR = 0.97
const HIGH_FACTOR = 1.03
const CLOSE_DEVIATION = 0.2

function guardedBarAmount(row: MinuteKlineRow): number | null {
  const raw = row.amount
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return null
  const volume = row.volume
  const close = row.close
  if (!Number.isFinite(volume) || volume <= 0 || !Number.isFinite(close) || close <= 0) return raw
  const shares = volume * LOT_SHARES
  const implied = raw / shares
  if (!Number.isFinite(implied)) return raw
  const low = row.low
  const high = row.high
  const bandOk = Number.isFinite(low) && low > 0 && Number.isFinite(high) && high > 0 && high >= low
  const outsideBand = bandOk && (implied < low * LOW_FACTOR || implied > high * HIGH_FACTOR)
  const farFromClose = Math.abs(implied / close - 1) > CLOSE_DEVIATION
  return outsideBand || farFromClose ? close * shares : raw
}

export function computeIntradayAverage(data: MinuteKlineRow[]): (number | null)[] {
  const result: (number | null)[] = []
  let amount = 0
  let volume = 0
  let hasAmount = true
  let dayLow = Number.POSITIVE_INFINITY
  let dayHigh = Number.NEGATIVE_INFINITY
  for (const row of data) {
    if (Number.isFinite(row.low) && row.low > 0) dayLow = Math.min(dayLow, row.low)
    if (Number.isFinite(row.high) && row.high > 0) dayHigh = Math.max(dayHigh, row.high)
    const barAmount = guardedBarAmount(row)
    if (barAmount != null) amount += barAmount
    else hasAmount = false
    volume += row.volume * LOT_SHARES
    if (!(hasAmount && volume > 0)) {
      result.push(null)
      continue
    }
    const average = amount / volume
    // 单根容差允许隐含价略出本根高低点，累计均价因此用同一条带，而不是严格的当日高低价。
    const inRange = !(dayLow < Number.POSITIVE_INFINITY && dayHigh > Number.NEGATIVE_INFINITY)
      || (average >= dayLow * LOW_FACTOR && average <= dayHigh * HIGH_FACTOR)
    result.push(inRange ? average : null)
  }
  return result
}

function generateFullDayTimes(): string[] {
  const times: string[] = []
  for (let hour = 9; hour <= 11; hour++) {
    const startMinute = hour === 9 ? 30 : 0
    const endMinute = hour === 11 ? 30 : 59
    for (let minute = startMinute; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  for (let hour = 13; hour <= 15; hour++) {
    const endMinute = hour === 15 ? 0 : 59
    for (let minute = 0; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  return times
}

export const FULL_DAY_TIMES = generateFullDayTimes()
