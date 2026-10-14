import { describe, expect, it } from 'vitest'
import { computeIntradayAverage } from './intraday-chart'
import type { MinuteKlineRow } from '@/lib/api'

function bar(close: number, volume = 100, amount?: number, low?: number, high?: number): MinuteKlineRow {
  return {
    datetime: '2026-10-13 09:31:00',
    open: close,
    high: high ?? close,
    low: low ?? close,
    close,
    volume,
    amount: amount ?? close * volume * 100,
  }
}

describe('分时均价线 amount 防护', () => {
  it('正常成交额的均价不变', () => {
    const rows = [bar(10), bar(10.2, 100, undefined, 10.1, 10.3), bar(9.8, 100, undefined, 9.7, 9.9)]
    const average = computeIntradayAverage(rows)
    expect(average[0]).toBeCloseTo(10)
    expect(average[1]).toBeCloseTo((10 + 10.2) / 2)
    expect(average[2]).toBeCloseTo((10 + 10.2 + 9.8) / 3)
  })

  it('920045.BJ 式 10 倍成交额不会把均价抬出当日高低价', () => {
    const dirty = bar(8.52, 1200, 8.52 * 1200 * 100 * 10, 8.4, 8.6)
    const rows = [bar(8.4, 800, undefined, 8.35, 8.45), dirty, bar(8.6, 900, undefined, 8.55, 8.65)]
    const average = computeIntradayAverage(rows)
    const dayLow = Math.min(...rows.map(row => row.low))
    const dayHigh = Math.max(...rows.map(row => row.high))
    for (const value of average) {
      expect(value).not.toBeNull()
      expect(value!).toBeGreaterThanOrEqual(dayLow)
      expect(value!).toBeLessThanOrEqual(dayHigh)
    }
    expect(average[1]).toBeCloseTo((8.4 * 800 + 8.52 * 1200) / (800 + 1200), 6)
  })

  it('缺成交额时后续均价仍为空', () => {
    const rows = [bar(10), { ...bar(10.2), amount: null }]
    expect(computeIntradayAverage(rows)[1]).toBeNull()
  })
})
