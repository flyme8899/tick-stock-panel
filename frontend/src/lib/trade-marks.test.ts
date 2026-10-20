import { describe, expect, it } from 'vitest'
import {
  candleMarkStatus,
  dsaLines,
  fillMarkers,
  formatForward,
  intradayMarkPoints,
  intradayTradeMarks,
  markerDetails,
  preferBarTMarks,
  strategyMarkers,
  type IntradayTradeMark,
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
  it('没有可画的K线标记时，区分未选策略和区间为空', () => {
    expect(candleMarkStatus({
      show: true, strategyOn: true, hasStrategy: false, paperOn: true,
      drawn: 0, pending: false, failed: false,
    })).toBe('选择策略后显示买卖点')
    expect(candleMarkStatus({
      show: true, strategyOn: true, hasStrategy: true, paperOn: true,
      drawn: 0, pending: false, failed: false,
    })).toBe('这段区间没有买卖点')
    expect(candleMarkStatus({
      show: true, strategyOn: false, hasStrategy: false, paperOn: true,
      drawn: 0, pending: false, failed: false,
    })).toBe('这段区间没有买卖点')
    expect(candleMarkStatus({
      show: true, strategyOn: true, hasStrategy: true, paperOn: false,
      drawn: 2, pending: false, failed: false,
    })).toBeNull()
    expect(candleMarkStatus({
      show: true, strategyOn: false, hasStrategy: false, paperOn: false,
      drawn: 0, pending: false, failed: false,
    })).toBeNull()
    expect(candleMarkStatus({
      show: true, strategyOn: true, hasStrategy: false, paperOn: true,
      drawn: 0, pending: true, failed: false,
    })).toBeNull()
    expect(candleMarkStatus({
      show: false, strategyOn: true, hasStrategy: false, paperOn: true,
      drawn: 0, pending: false, failed: false,
    })).toBeNull()
  })

  it('远期收益不足时显示破折号', () => {
    expect(formatForward(null)).toBe('—')
    expect(formatForward(undefined)).toBe('—')
    expect(formatForward(Number.NaN)).toBe('—')
    expect(formatForward(0.03)).toBe('+3.00%')
    expect(formatForward(-0.012)).toBe('-1.20%')
  })

  it('策略买点为红三角，突破为菱形，卖点为绿三角', () => {
    const marks = strategyMarkers([
      marker(),
      marker({ id: 'buy:2024-03-04:breakout', date: '2024-03-04', style: 'breakout' }),
      marker({ id: 'sell:2024-03-06:triangle', date: '2024-03-06', side: 'sell', style: 'triangle' }),
    ], true)
    expect(marks.map(item => [item.style, item.kind, item.color, item.markerId])).toEqual([
      ['triangle', 'buy', '#F04438', 'buy:2024-03-01:triangle'],
      ['breakout', 'buy', '#F04438', 'buy:2024-03-04:breakout'],
      ['triangle', 'sell', '#12B76A', 'sell:2024-03-06:triangle'],
    ])
    expect(strategyMarkers([marker()], false)).toEqual([])
  })

  it('反向信号画成绿跌风险点，不画红涨买点', () => {
    const marks = strategyMarkers([
      marker({ id: 'risk:2024-03-01', side: 'risk', style: 'risk', rule: '历史上该信号后 20 日显著跑输行业' }),
    ], true)
    expect(marks).toEqual([{
      date: '2024-03-01',
      kind: 'neutral',
      label: '险',
      markerId: 'risk:2024-03-01',
      color: '#12B76A',
    }])
  })

  it('模拟盘成交画成 B/S 圆点', () => {
    const fills: TradeFill[] = [
      { date: '2024-03-01', side: 'buy', price: 10.5, qty: 100, account: 'default' },
      { date: '2024-03-08', side: 'sell', price: 11, qty: 100, account: 'default' },
    ]
    const marks = fillMarkers(fills, true)
    expect(marks[0]).toMatchObject({ style: 'circle', kind: 'buy', color: '#F04438' })
    expect(marks[1]).toMatchObject({ style: 'circle', kind: 'sell', color: '#12B76A' })
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
    const sniper: TradeLevel[] = [
      { kind: 'buy', label: '理想买入', price: 1800 },
      { kind: 'stop', label: '止损', price: 1700 },
      { kind: 'target', label: '目标价', price: 1900 },
    ]
    expect(dsaLines(sniper, true).map(line => [line.label, line.color])).toEqual([
      ['理想买入 1800.00', '#F04438'],
      ['止损 1700.00', '#12B76A'],
      ['目标价 1900.00', '#A855F7'],
    ])
    const mixed: TradeLevel[] = [
      { kind: 'target', label: '目标价', price: 1900 },
      { kind: 'resistance', label: '压力', price: 1910 },
    ]
    const colors = dsaLines(mixed, true).map(line => line.color)
    expect(colors).toEqual(['#A855F7', '#F97316'])
    expect(dsaLines(sniper, true, true).map(line => [line.label, line.color])).toEqual([
      ['买入 1800.00', '#F04438'],
      ['止损 1700.00', '#12B76A'],
      ['目标 1900.00', '#A855F7'],
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

  it('分时标记复用日K字段，关开关或没有分钟时不画', () => {
    const marks = intradayTradeMarks({
      showStrategy: true,
      showPaper: true,
      showT: true,
      date: '2024-03-01',
      markers: [
        marker({ time: null }),
        marker({ id: 'other', date: '2024-03-02', side: 'sell' }),
      ],
      fills: [
        { date: '2024-03-01', side: 'buy', price: 10.5, qty: 100, account: 'default', time: '10:15:08' },
        { date: '2024-03-01', side: 'sell', price: 11, qty: 100, account: 'default', time: null },
      ],
      tMarks: [
        { time: '09:36', rule: '高于分时均价', side: 'sell', price: 10.4 },
        { time: '', rule: '缺分钟', side: 'buy', price: 10 },
      ],
      tDate: '2024-03-01',
    })
    expect(marks.map(item => [item.source, item.time, item.style, item.color, item.tip])).toEqual([
      ['strategy', '15:00', 'triangle', '#F04438', '策略信号 · 买入 10.50'],
      ['paper', '10:15', 'circle', '#F04438', '模拟成交 · 买入 10.50'],
      ['paper', '15:00', 'circle', '#12B76A', '模拟成交 · 卖出 11.00'],
      ['t', '09:36', 'dot', '#12B76A', '做T提醒 · 高于分时均价 10.40'],
    ])
    expect(intradayTradeMarks({ showStrategy: false, showPaper: false, showT: false, markers: [marker()] })).toEqual([])
    expect(intradayTradeMarks({ showT: true, tMarks: null })).toEqual([])
    const placed = intradayMarkPoints(marks, mark => (
      mark.time === '09:36' ? null : { x: mark.time, y: mark.price ?? Number.NaN }
    ))
    expect(placed.map(item => [item.symbol, (item.itemStyle as { color: string }).color, item.coord])).toEqual([
      ['triangle', '#F04438', ['15:00', 10.5]],
      ['circle', '#F04438', ['10:15', 10.5]],
      ['circle', '#12B76A', ['15:00', 11]],
    ])
    expect(intradayMarkPoints([], () => null)).toEqual([])
    const bar: IntradayTradeMark = {
      date: '2024-03-01', time: '10:00', side: 'buy', style: 'dot', color: '#F04438',
      tip: '做T提醒 · 低于分时均价 10.00', price: 10, source: 't', label: 'T',
    }
    const merged = preferBarTMarks(marks, [bar], ['2024-03-01'])
    expect(merged.filter(item => item.source === 't')).toEqual([bar])
    expect(merged.some(item => item.source === 'strategy')).toBe(true)
  })
})
