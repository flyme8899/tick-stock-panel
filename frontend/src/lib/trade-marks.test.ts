import { describe, expect, it } from 'vitest'
import {
  dsaLines,
  fillMarkers,
  formatForward,
  markerDetails,
  strategyMarkers,
  type TradeFill,
  type TradeLevel,
  type TradeSignalMarker,
} from './trade-marks'

const marker = (patch: Partial<TradeSignalMarker> = {}): TradeSignalMarker => ({
  id: 'buy:2024-03-01:triangle',
  date: '2024-03-01',
  side: 'buy',
  style: 'triangle',
  rule: '趋势突破 · 收盘突破',
  price: 10.5,
  fwd5: 0.03,
  fwd20: null,
  ...patch,
})

describe('买卖点标记映射', () => {
  it('远期收益不足时显示破折号', () => {
    expect(formatForward(null)).toBe('—')
    expect(formatForward(undefined)).toBe('—')
    expect(formatForward(Number.NaN)).toBe('—')
    expect(formatForward(0.03)).toBe('+3.00%')
    expect(formatForward(-0.012)).toBe('-1.20%')
  })

  it('策略买点为绿三角，突破为菱形，卖点为红三角', () => {
    const marks = strategyMarkers([
      marker(),
      marker({ id: 'buy:2024-03-04:breakout', date: '2024-03-04', style: 'breakout' }),
      marker({ id: 'sell:2024-03-06:triangle', date: '2024-03-06', side: 'sell', style: 'triangle' }),
    ], true)
    expect(marks.map(item => [item.style, item.kind, item.color, item.markerId])).toEqual([
      ['triangle', 'buy', '#12B76A', 'buy:2024-03-01:triangle'],
      ['breakout', 'buy', '#12B76A', 'buy:2024-03-04:breakout'],
      ['triangle', 'sell', '#F04438', 'sell:2024-03-06:triangle'],
    ])
    expect(strategyMarkers([marker()], false)).toEqual([])
  })

  it('模拟盘成交画成 B/S 圆点', () => {
    const fills: TradeFill[] = [
      { date: '2024-03-01', side: 'buy', price: 10.5, qty: 100, account: 'default' },
      { date: '2024-03-08', side: 'sell', price: 11, qty: 100, account: 'default' },
    ]
    const marks = fillMarkers(fills, true)
    expect(marks[0]).toMatchObject({ style: 'circle', kind: 'buy', color: '#12B76A' })
    expect(marks[1]).toMatchObject({ style: 'circle', kind: 'sell', color: '#F04438' })
    expect(fillMarkers(fills, false)).toEqual([])
  })

  it('没有 DSA 报告或开关关闭时不画价位线', () => {
    const levels: TradeLevel[] = [
      { kind: 'support', label: '支撑', price: 1680 },
      { kind: 'resistance', label: '压力', price: 1900.5 },
      { kind: 'stop', label: '止损', price: 1700 },
    ]
    expect(dsaLines([], true)).toEqual([])
    expect(dsaLines(levels, false)).toEqual([])
    expect(dsaLines(levels, true).map(line => line.label)).toEqual([
      '支撑 1680.00',
      '压力 1900.50',
      '止损 1700.00',
    ])
  })

  it('悬停卡片能对上策略规则和模拟盘成交', () => {
    const fills: TradeFill[] = [
      { date: '2024-03-01', side: 'buy', price: 10.5, qty: 100, account: 'default' },
    ]
    const details = markerDetails([marker()], fills)
    expect(details.get('buy:2024-03-01:triangle')).toMatchObject({
      rule: '趋势突破 · 收盘突破',
      fwd5: 0.03,
      fwd20: null,
    })
    const fill = details.get('fill:default:2024-03-01:buy:10.5')
    expect(fill?.rule).toContain('模拟盘成交')
    expect(fill?.fwd20).toBeNull()
  })
})
