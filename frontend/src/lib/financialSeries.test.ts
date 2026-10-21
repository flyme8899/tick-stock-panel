import { describe, expect, it } from 'vitest'
import {
  DEFAULT_RECENT_PERIODS,
  amountUnit,
  applyWindow,
  buildAnnualBundle,
  buildSeries,
  dedupeByPeriod,
  isAnnualBundleEmpty,
  isAnnualPeriod,
  isSeriesEmpty,
  mergeEps,
  axisLabels,
  periodLabel,
  sortAscByPeriod,
  toUnit,
  usablePeriodCount,
  windowLabel,
} from './financialSeries'

/** 造一期 metrics 记录; 只给关心的字段, 其余留空。 */
function m(period: string, patch: Record<string, any> = {}): Record<string, any> {
  return { symbol: '600519', period_end: period, announce_date: period, ...patch }
}

/** 造一期 income 记录。 */
function inc(period: string, patch: Record<string, any> = {}): Record<string, any> {
  return { symbol: '600519', period_end: period, announce_date: period, ...patch }
}

describe('dedupeByPeriod — 同源 period_end 去重', () => {
  it('同一报告期只保留一条', () => {
    const rows = [m('2024-12-31', { roe: 10 }), m('2024-12-31', { roe: 20 }), m('2025-03-31', { roe: 5 })]
    const out = dedupeByPeriod(rows)
    expect(out).toHaveLength(2)
    expect(out.map(r => r.period_end)).toEqual(['2024-12-31', '2025-03-31'])
  })

  it('保留 announce_date 更新的那条(财报更正/重述以新公告为准)', () => {
    const rows = [
      m('2024-12-31', { roe: 10, announce_date: '2025-04-20' }),
      m('2024-12-31', { roe: 12, announce_date: '2025-06-15' }),
    ]
    const out = dedupeByPeriod(rows)
    expect(out).toHaveLength(1)
    expect(out[0].roe).toBe(12)
  })

  it('announce_date 相同或都缺失时保留数组里更靠后的一条', () => {
    const same = dedupeByPeriod([
      m('2024-12-31', { roe: 10, announce_date: '2025-04-20' }),
      m('2024-12-31', { roe: 11, announce_date: '2025-04-20' }),
    ])
    expect(same[0].roe).toBe(11)

    const none = dedupeByPeriod([
      m('2024-12-31', { roe: 10, announce_date: null }),
      m('2024-12-31', { roe: 11, announce_date: null }),
    ])
    expect(none[0].roe).toBe(11)
  })

  it('丢弃没有 period_end 的行, 空入参返回空数组', () => {
    expect(dedupeByPeriod([m(''), m('2024-12-31')])).toHaveLength(1)
    expect(dedupeByPeriod([])).toEqual([])
    expect(dedupeByPeriod(null)).toEqual([])
    expect(dedupeByPeriod(undefined)).toEqual([])
  })

  it('去重后不改变原有相对顺序', () => {
    const out = dedupeByPeriod([m('2025-03-31'), m('2024-12-31'), m('2025-03-31')])
    expect(out.map(r => r.period_end)).toEqual(['2025-03-31', '2024-12-31'])
  })
})

describe('排序与年报判定', () => {
  it('sortAscByPeriod 升序(左旧右新)', () => {
    const out = sortAscByPeriod([m('2025-03-31'), m('2023-12-31'), m('2024-09-30')])
    expect(out.map(r => r.period_end)).toEqual(['2023-12-31', '2024-09-30', '2025-03-31'])
  })

  it('isAnnualPeriod 只认 12-31 结尾', () => {
    expect(isAnnualPeriod('2024-12-31')).toBe(true)
    expect(isAnnualPeriod('2024-09-30')).toBe(false)
    expect(isAnnualPeriod('2024-06-30')).toBe(false)
    expect(isAnnualPeriod('2024-03-31')).toBe(false)
    expect(isAnnualPeriod('')).toBe(false)
  })

  it('periodLabel 年报写年份, 其余写年月', () => {
    expect(periodLabel('2024-12-31')).toBe('2024')
    expect(periodLabel('2024-09-30')).toBe('2024-09')
  })

  it('axisLabels: 纯年报时只写年份', () => {
    expect(axisLabels(['2022-12-31', '2023-12-31'])).toEqual(['2022', '2023'])
  })

  it('axisLabels: 同一年里年报与季报并存 → 退化成 YY-MM, 避免两个"2022"', () => {
    const got = axisLabels(['2022-03-31', '2022-06-30', '2022-09-30', '2022-12-31'])
    expect(got).toEqual(['22-03', '22-06', '22-09', '22-12'])
    expect(new Set(got).size).toBe(got.length)
  })

  it('axisLabels: 某年只有季报、另一年混排 → 只把混排的那年退化', () => {
    expect(axisLabels(['2021-09-30', '2022-03-31', '2022-12-31']))
      .toEqual(['2021-09', '22-03', '22-12'])
  })

  it('axisLabels: 标签全表唯一(混排场景)', () => {
    const mixed = ['2023-06-30', '2023-12-31', '2024-03-31', '2024-12-31']
    const got = axisLabels(mixed)
    expect(new Set(got).size).toBe(got.length)
  })
})

describe('applyWindow — 三种窗口切换', () => {
  const yearEnds = ['2018-12-31', '2019-12-31', '2020-12-31', '2021-12-31']
  const quarters = ['2025-03-31', '2025-06-30', '2025-09-30', '2025-12-31']
  const all = [...yearEnds, ...quarters].map(p => m(p))

  it('only-annual: 仅保留 12-31', () => {
    const out = applyWindow(all, 'annual')
    expect(out.map(r => r.period_end)).toEqual([...yearEnds, '2025-12-31'])
    expect(out.every(r => isAnnualPeriod(r.period_end))).toBe(true)
  })

  it('recent: 取升序后的最后 N 期', () => {
    const out = applyWindow(all, 'recent', 3)
    expect(out.map(r => r.period_end)).toEqual(['2025-06-30', '2025-09-30', '2025-12-31'])
  })

  it('recent: 期数不足 N 时全给', () => {
    const out = applyWindow(all, 'recent', 100)
    expect(out).toHaveLength(all.length)
  })

  it('recent: N 非法时回落默认 12', () => {
    const many = Array.from({ length: 20 }, (_, i) => m(`${2000 + i}-12-31`))
    expect(applyWindow(many, 'recent', 0)).toHaveLength(DEFAULT_RECENT_PERIODS)
    expect(applyWindow(many, 'recent', Number.NaN)).toHaveLength(DEFAULT_RECENT_PERIODS)
  })

  it('all: 原样返回', () => {
    expect(applyWindow(all, 'all')).toHaveLength(all.length)
  })

  it('空数组任何窗口都返回空', () => {
    expect(applyWindow([], 'annual')).toEqual([])
    expect(applyWindow([], 'recent', 8)).toEqual([])
    expect(applyWindow([], 'all')).toEqual([])
  })

  it('windowLabel 文案', () => {
    expect(windowLabel('recent', 8)).toBe('近 8 期')
    expect(windowLabel('all')).toBe('全部')
    expect(windowLabel('annual')).toBe('仅年报')
  })
})

describe('buildSeries — 组装双折线', () => {
  it('labels/values 等长同序, 缺失保留 null 不填 0', () => {
    const rows = [
      m('2024-12-31', { gross_margin: 91.2, net_margin: 50.1 }),
      m('2025-03-31', { gross_margin: 92.0, net_margin: null }),
    ]
    const b = buildSeries(rows, [
      { key: 'gross_margin', name: '毛利率' },
      { key: 'net_margin', name: '净利率' },
    ])
    expect(b.labels).toEqual(['2024', '2025-03'])
    expect(b.isAnnual).toEqual([true, false])
    expect(b.series[0].values).toEqual([91.2, 92.0])
    // 关键: 缺失是 null, 绝不能变 0(0 会在图上画出一条假的下探)
    expect(b.series[1].values).toEqual([50.1, null])
  })

  it('字符串数字与非法值处理', () => {
    const b = buildSeries(
      [m('2024-12-31', { a: '3.5', b: '', c: 'abc', d: undefined })],
      [{ key: 'a', name: 'a' }, { key: 'b', name: 'b' }, { key: 'c', name: 'c' }, { key: 'd', name: 'd' }],
    )
    expect(b.series.map(s => s.values[0])).toEqual([3.5, null, null, null])
  })

  it('字段全空 → isSeriesEmpty 为真', () => {
    const b = buildSeries([m('2024-12-31'), m('2025-03-31')], [{ key: 'roe', name: 'ROE' }])
    expect(isSeriesEmpty(b)).toBe(true)
    expect(usablePeriodCount(b)).toBe(0)
  })

  it('只有部分期有值 → 不算全空, 可用期数按"任一序列有值"计', () => {
    const b = buildSeries(
      [m('2024-12-31', { roe: 10 }), m('2025-03-31'), m('2025-06-30', { roe: 11 })],
      [{ key: 'roe', name: 'ROE' }],
    )
    expect(isSeriesEmpty(b)).toBe(false)
    expect(usablePeriodCount(b)).toBe(2)
  })

  it('空行 → 空 bundle', () => {
    const b = buildSeries([], [{ key: 'roe', name: 'ROE' }])
    expect(b.labels).toEqual([])
    expect(isSeriesEmpty(b)).toBe(true)
    expect(usablePeriodCount(b)).toBe(0)
  })

  it('null bundle 不崩', () => {
    expect(isSeriesEmpty(null)).toBe(true)
    expect(usablePeriodCount(null)).toBe(0)
  })
})

describe('mergeEps — EPS 回落', () => {
  it('优先 metrics.eps_basic', () => {
    const map = mergeEps([m('2024-12-31', { eps_basic: 1.5 })], [inc('2024-12-31', { basic_eps: 2.0 })])
    expect(map.get('2024-12-31')).toBe(1.5)
  })

  it('metrics 缺该期 → 回落 income.basic_eps', () => {
    const map = mergeEps([], [inc('2024-12-31', { basic_eps: 2.0 })])
    expect(map.get('2024-12-31')).toBe(2.0)
  })

  it('metrics 该期值为 null 也回落', () => {
    const map = mergeEps(
      [m('2024-12-31', { eps_basic: null })],
      [inc('2024-12-31', { basic_eps: 2.0 })],
    )
    expect(map.get('2024-12-31')).toBe(2.0)
  })

  it('两边都没有 → null(不是 0)', () => {
    const map = mergeEps([m('2024-12-31')], [inc('2024-12-31')])
    expect(map.get('2024-12-31')).toBeNull()
  })

  it('两表期数不同 → 并集', () => {
    const map = mergeEps([m('2024-12-31', { eps_basic: 1 })], [inc('2025-12-31', { basic_eps: 2 })])
    expect(Array.from(map.keys()).sort()).toEqual(['2024-12-31', '2025-12-31'])
  })
})

describe('buildAnnualBundle — 年报金额图', () => {
  it('只出年报, 三序列按年对齐', () => {
    const b = buildAnnualBundle(
      [m('2024-12-31', { eps_basic: 1.2 }), m('2025-03-31', { eps_basic: 0.3 }), m('2025-12-31', { eps_basic: 1.5 })],
      [
        inc('2024-12-31', { revenue: 1.5e10, net_income_attributable: 4.0e9 }),
        inc('2025-03-31', { revenue: 4e9, net_income_attributable: 1e9 }),
        inc('2025-12-31', { revenue: 1.8e10, net_income_attributable: 5.0e9 }),
      ],
    )
    expect(b.periods).toEqual(['2024-12-31', '2025-12-31'])
    expect(b.labels).toEqual(['2024', '2025'])
    expect(b.eps).toEqual([1.2, 1.5])
    expect(b.revenue).toEqual([1.5e10, 1.8e10])
    expect(b.netIncome).toEqual([4.0e9, 5.0e9])
  })

  it('某字段缺失的年份不会整年消失', () => {
    const b = buildAnnualBundle(
      [m('2023-12-31', { eps_basic: 0.9 })],
      [inc('2024-12-31', { revenue: 1.5e10, net_income_attributable: 4.0e9 })],
    )
    expect(b.periods).toEqual(['2023-12-31', '2024-12-31'])
    expect(b.eps).toEqual([0.9, null])
    expect(b.revenue).toEqual([null, 1.5e10])
  })

  it('三序列全空的年份不出现', () => {
    const b = buildAnnualBundle([m('2022-12-31'), m('2024-12-31', { eps_basic: 1 })], [])
    expect(b.periods).toEqual(['2024-12-31'])
  })

  it('完全没有年报 → 空 bundle', () => {
    const b = buildAnnualBundle([m('2025-03-31', { eps_basic: 0.3 })], [inc('2025-06-30', { revenue: 1 })])
    expect(b.periods).toEqual([])
    expect(isAnnualBundleEmpty(b)).toBe(true)
  })

  it('空入参不崩', () => {
    const b = buildAnnualBundle([], [])
    expect(isAnnualBundleEmpty(b)).toBe(true)
    expect(isAnnualBundleEmpty(null)).toBe(true)
  })
})

describe('amestUnit — 金额单位对齐 fmtBigNum', () => {
  it('≥1e12 用万亿', () => {
    expect(amountUnit([2.5e12]).suffix).toBe('万亿')
  })
  it('≥1e8 用亿', () => {
    expect(amountUnit([1.8e10]).suffix).toBe('亿')
    expect(amountUnit([1.8e10]).div).toBe(1e8)
  })
  it('≥1e4 用万', () => {
    expect(amountUnit([56000]).suffix).toBe('万')
  })
  it('不足万不换算', () => {
    const u = amountUnit([1200])
    expect(u.div).toBe(1)
    expect(u.suffix).toBe('')
  })
  it('按最大绝对值定单位, 含负数', () => {
    // 有亏损年份时净利为负, 单位要按绝对值挑
    expect(amountUnit([-1.2e10, 3e8]).suffix).toBe('亿')
  })
  it('全空 → 不换算', () => {
    expect(amountUnit([null, null]).div).toBe(1)
    expect(amountUnit([]).div).toBe(1)
  })
  it('toUnit 保留 null(断线), 其余按除数换算', () => {
    const u = amountUnit([1.8e10])
    expect(toUnit(null, u)).toBeNull()
    expect(toUnit(1.8e10, u)).toBe(180)
  })
})
