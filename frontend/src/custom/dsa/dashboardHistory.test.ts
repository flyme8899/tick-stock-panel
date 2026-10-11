import { expect, it } from 'vitest'
import {
  dedupeMarketReviewCards,
  formatPercentPoints,
  nameLookupSymbols,
  namesByWatchKey,
  poolName,
  poolSnapshot,
  resolvePoolName,
  toDashboardCard,
  watchKey,
  type DashboardCard,
} from './dashboardHistory'

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
    price: null,
    changePctPoints: null,
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

it('does not treat the code as a stock name and reads percent-point quotes', () => {
  const coded = card({
    id: 9,
    stockCode: '600105',
    stockName: '600105',
    reportType: 'detailed',
    summary: '代码当名称',
    createdAt: '2026-10-09 18:00',
    price: 8.42,
    changePctPoints: 3.25,
  })
  const named = card({
    id: 10,
    stockCode: '600105.SH',
    stockName: '永鼎股份',
    reportType: 'detailed',
    summary: '有名称',
    createdAt: '2026-10-10 18:00',
    price: 9,
    changePctPoints: -1.2,
  })
  expect(poolName('600105', [coded])).toBe('600105')
  expect(poolName('600105', [coded, named])).toBe('永鼎股份')

  const fromMap = namesByWatchKey({ '600105.SH': '永鼎股份', '000001.SZ': '000001.SZ' })
  expect(resolvePoolName('600105', [coded], fromMap)).toBe('永鼎股份')
  expect(resolvePoolName('688999', [coded], fromMap)).toBe('688999')
  expect(fromMap.has('000001')).toBe(false)
  expect(nameLookupSymbols('600105')).toEqual(['600105.SH', '600105.SZ', '600105.BJ'])
  expect(nameLookupSymbols('hk00700')).toEqual(['00700.HK'])

  const parsed = toDashboardCard({
    stock_code: '600105',
    stock_name: '600105',
    current_price: 8.42,
    change_pct: 3.25,
  })
  expect(parsed.price).toBe(8.42)
  expect(parsed.changePctPoints).toBe(3.25)
  expect(formatPercentPoints(3.25)).toBe('+3.25%')
  expect(formatPercentPoints(-1.8)).toBe('-1.80%')
  // 卡片按调用方顺序查找，最新且带报价的记录优先。dedupe 后的列表是新的在前。
  expect(poolSnapshot('600105', [named, coded])).toEqual({ price: 9, changePctPoints: -1.2 })
  expect(poolSnapshot('600105', [coded])).toEqual({ price: 8.42, changePctPoints: 3.25 })
})
