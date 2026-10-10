import { expect, it } from 'vitest'
import { dedupeMarketReviewCards, poolName, toDashboardCard, watchKey, type DashboardCard } from './dashboardHistory'

function card(partial: Partial<DashboardCard> & Pick<DashboardCard, 'id' | 'summary' | 'createdAt'>): DashboardCard {
  return {
    stockCode: 'MARKET',
    stockName: '大盘复盘',
    advice: '查看复盘',
    score: '50',
    trend: '大盘复盘',
    region: 'cn',
    read: false,
    reportType: 'market_review',
    marketDay: null,
    ...partial,
  }
}

it('keeps the latest market review for one calendar day', () => {
  const morning = card({ id: 1, summary: '2026-10-10 大盘复盘 早盘', createdAt: '2026-10-10 15:10' })
  const closing = card({ id: 2, summary: '2026-10-10 大盘复盘 收盘', createdAt: '2026-10-10 18:40' })
  const stock = card({
    id: 3,
    stockCode: '603626.SH',
    stockName: '科森科技',
    reportType: 'detailed',
    summary: '均线多头',
    createdAt: '2026-10-10 18:20',
    advice: '观望',
  })
  morning.marketDay = '2026-10-10'
  closing.marketDay = '2026-10-10'

  const visible = dedupeMarketReviewCards([morning, stock, closing])

  expect(visible.map(item => item.id)).toEqual([2, 3])
})

it('reads the heading date and the watchlist equivalence key', () => {
  const row = toDashboardCard({
    id: 8,
    stock_code: 'MARKET',
    stock_name: '大盘复盘',
    report_type: 'market_review',
    analysis_summary: '2026年10月10日 大盘复盘',
    created_at: '2026-10-10T18:40:00+08:00',
    read_at: '2026-10-10T19:00:00+08:00',
  })
  expect(row.marketDay).toBe('2026-10-10')
  expect(row.read).toBe(true)
  expect(watchKey('603626.SH')).toBe(watchKey('SH603626'))
  expect(watchKey('00700.HK')).toBe('HK00700')
  expect(poolName('603626', [
    card({
      id: 4,
      stockCode: '603626.SH',
      stockName: '科森科技',
      reportType: 'detailed',
      summary: '观察',
      createdAt: '2026-10-10 18:00',
    }),
  ])).toBe('科森科技')
})
