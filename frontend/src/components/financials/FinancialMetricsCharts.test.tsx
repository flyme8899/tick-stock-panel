// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FinancialMetricsCharts, buildAnnualOption, buildPercentOption, buildYoyOption, colorOf, segmentLines } from './FinancialMetricsCharts'
import { buildAnnualBundle, buildSeries, type SeriesBundle } from '@/lib/financialSeries'
import { chartTheme } from '@/lib/theme'

// echarts 在 jsdom 里没有 canvas, 用假实例接住 init/setOption, 只验证调用与 option 形状
const chart = vi.hoisted(() => ({
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
}))
vi.mock('echarts', () => ({ init: () => chart }))

// jsdom 没有 ResizeObserver
class RO {
  observe() {}
  disconnect() {}
}
;(globalThis as any).ResizeObserver = RO

const CT = chartTheme('dark')

function bundle(values: (number | null)[][], labels: string[]): SeriesBundle {
  return {
    labels,
    periods: labels,
    isAnnual: labels.map(l => l.length === 4),
    series: values.map((v, i) => ({ name: `S${i}`, values: v })),
  }
}

describe('buildPercentOption', () => {
  it('null 保持原样(断线), 不当 0 画', () => {
    const opt: any = buildPercentOption(bundle([[1.2, null]], ['2024', '2025']), CT, ['#111', '#222'])
    expect(opt.series[0].data).toEqual([1.2, null])
    expect(opt.series[0].connectNulls).toBe(false)
  })

  it('两条线各取一个颜色', () => {
    const opt: any = buildPercentOption(bundle([[1], [2]], ['2024']), CT, ['#111', '#222'])
    expect(opt.series.map((s: any) => s.lineStyle.color)).toEqual(['#111', '#222'])
  })
})

describe('buildYoyOption', () => {
  it('不再使用 visualMap(pieces 会崩, inRange 会渐变失效)', () => {
    const opt: any = buildYoyOption(bundle([[5, -5], [3, -3]], ['2024', '2025']), CT)
    expect(opt.visualMap).toBeUndefined()
  })

  it('折线本身由逐段 markLine 画, 主系列线宽为 0', () => {
    const opt: any = buildYoyOption(bundle([[5, -5], [3, -3]], ['2024', '2025']), CT)
    expect(opt.series[0].lineStyle.width).toBe(0)
    // 两点一段 → 1 段
    expect(opt.series[0].markLine.data).toHaveLength(1)
  })

  it('两条序列用形状+线型区分(颜色已被正负占用)', () => {
    const opt: any = buildYoyOption(bundle([[5, -5], [3, -3]], ['2024', '2025']), CT)
    expect(opt.series[0].symbol).toBe('circle')
    expect(opt.series[1].symbol).toBe('rect')
    expect(opt.series[0].markLine.data[0][0].lineStyle.type).toBe('solid')
    expect(opt.series[1].markLine.data[0][0].lineStyle.type).toBe('dashed')
  })

  it('主系列保留真实值, tooltip 不会读错', () => {
    const opt: any = buildYoyOption(bundle([[5, null]], ['2024', '2025']), CT)
    expect(opt.series[0].data).toEqual([5, null])
  })
})

describe('colorOf', () => {
  it('正红负绿, 0 与缺失走中性色', () => {
    expect(colorOf(3)).toBe('#C74040')
    expect(colorOf(-3)).toBe('#2D9B65')
    expect(colorOf(0)).toBe('#71717A')
    expect(colorOf(null)).toBe('#71717A')
    expect(colorOf(undefined)).toBe('#71717A')
  })
})

describe('segmentLines — 逐段按正负着色', () => {
  it('两端同号用该号颜色', () => {
    const segs = segmentLines(['a', 'b', 'c'], [5, 8, -2], true)
    expect(segs).toHaveLength(2)
    expect(segs[0][0].lineStyle.color).toBe('#C74040')   // 5 → 8 都是正
    expect(segs[1][0].lineStyle.color).toBe('#2D9B65')   // 8 → -2 取终点(负)
  })

  it('跨 0 取终点符号, 不做 0 轴截断(不凭空多出拐点)', () => {
    const segs = segmentLines(['a', 'b'], [-5, 6], true)
    expect(segs).toHaveLength(1)
    expect(segs[0][0].lineStyle.color).toBe('#C74040')
    // 段端点用的是真实值, 不是 0
    expect(segs[0][0].coord).toEqual(['a', -5])
    expect(segs[0][1].coord).toEqual(['b', 6])
  })

  it('null 处断开, 不跨断点连线', () => {
    const segs = segmentLines(['a', 'b', 'c', 'd'], [5, null, 6, 7], true)
    // 只有 c→d 一段, a→b 与 b→c 都不该出现
    expect(segs).toHaveLength(1)
    expect(segs[0][0].coord).toEqual(['c', 6])
    expect(segs[0][1].coord).toEqual(['d', 7])
  })

  it('全部为 null → 无段', () => {
    expect(segmentLines(['a', 'b'], [null, null], true)).toEqual([])
  })

  it('单点 → 无段(少于 2 期时上层本来就给占位)', () => {
    expect(segmentLines(['a'], [5], true)).toEqual([])
  })

  it('0 值视作非负, 与 colorOf 口径一致', () => {
    const segs = segmentLines(['a', 'b'], [0, 4], true)
    expect(segs[0][0].lineStyle.color).toBe('#C74040')
  })
})

describe('buildAnnualOption', () => {
  const b = buildAnnualBundle(
    [{ period_end: '2023-12-31', eps_basic: 1.1 }, { period_end: '2024-12-31', eps_basic: 1.5 }],
    [
      { period_end: '2023-12-31', revenue: 1.2e10, net_income_attributable: 4.0e9 },
      { period_end: '2024-12-31', revenue: 1.6e10, net_income_attributable: 5.0e9 },
    ],
  )

  it('金额柱走右轴, EPS 线走左轴(量级差太大, 不能共用一根轴)', () => {
    const opt: any = buildAnnualOption(b, CT)
    expect(opt.series[0].yAxisIndex).toBe(1)
    expect(opt.series[1].yAxisIndex).toBe(1)
    expect(opt.series[2].yAxisIndex).toBe(0)
    expect(opt.series[2].type).toBe('line')
    expect(opt.series[0].type).toBe('bar')
  })

  it('金额按整根轴统一换成亿, 不做逐点换算', () => {
    const opt: any = buildAnnualOption(b, CT)
    expect(opt.yAxis[1].name).toContain('亿')
    expect(opt.series[0].data).toEqual([120, 160])
    expect(opt.series[1].data).toEqual([40, 50])
  })

  it('EPS 保持元/股原值', () => {
    const opt: any = buildAnnualOption(b, CT)
    expect(opt.series[2].data).toEqual([1.1, 1.5])
  })

  it('tooltip 用 fmtBigNum 的原始口径, 不受右轴单位影响', () => {
    const opt: any = buildAnnualOption(b, CT)
    const html = opt.tooltip.formatter([{ dataIndex: 1 }])
    expect(html).toContain('160.00亿')
    expect(html).toContain('50.00亿')
    expect(html).toContain('1.50 元')
  })

  it('亏损年份的 tooltip 与柱子口径一致 (负数也要换单位)', () => {
    // 亏损年报是常态: 柱子按亿元画 (-3.2), tooltip 若把 -3.2e8 原样打出 -320000000,
    // 同一条 tooltip 里就会出现「柱子 -3.2 / 文字 -320000000」的割裂。
    const loss = buildAnnualBundle(
      [{ period_end: '2023-12-31', eps_basic: -0.8 }, { period_end: '2024-12-31', eps_basic: 0.3 }],
      [
        { period_end: '2023-12-31', revenue: 2.4e9, net_income_attributable: -3.2e8 },
        { period_end: '2024-12-31', revenue: 3.1e9, net_income_attributable: 1.1e8 },
      ],
    )
    const opt: any = buildAnnualOption(loss, CT)
    const html: string = opt.tooltip.formatter([{ dataIndex: 0 }])
    expect(html).toContain('-3.20亿')
    expect(html).toContain('24.00亿')
    expect(html).not.toContain('-320000000')
    expect(html).toContain('-0.80 元')
  })
})

describe('FinancialMetricsCharts 渲染', () => {
  let host: HTMLDivElement
  let root: ReturnType<typeof createRoot>

  beforeEach(() => {
    chart.setOption.mockClear()
    chart.dispose.mockClear()
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(() => {
    act(() => root.unmount())
    host.remove()
  })

  const periods = ['2022-12-31', '2023-12-31', '2024-12-31']
  const metrics = periods.map((p, i) => ({
    period_end: p,
    announce_date: p,
    gross_margin: 90 + i,
    net_margin: 50 + i,
    revenue_yoy: i === 1 ? -5 : 10,
    net_income_yoy: i === 1 ? -8 : 12,
    eps_basic: 1 + i,
  }))
  const income = periods.map((p, i) => ({
    period_end: p,
    revenue: 1e10 + i * 1e9,
    net_income_attributable: 3e9 + i * 2e8,
  }))

  it('有数据时渲染三个图, 不抛错', () => {
    act(() => root.render(<FinancialMetricsCharts metrics={metrics} income={income} window="recent" recentN={12} />))
    expect(host.textContent).toContain('毛利率 / 净利率')
    expect(host.textContent).toContain('营收同比 / 净利同比')
    expect(host.textContent).toContain('年报金额')
    // 三张图各 init 一次
    expect(chart.setOption).toHaveBeenCalledTimes(3)
  })

  it('全空数据 → 三块都给占位文案, 不崩', () => {
    act(() => root.render(<FinancialMetricsCharts metrics={[]} income={[]} window="recent" />))
    expect(host.textContent).toContain('暂无毛利率/净利率数据')
    expect(host.textContent).toContain('暂无同比数据')
    expect(host.textContent).toContain('暂无年报数据')
    expect(chart.setOption).not.toHaveBeenCalled()
  })

  it('null 入参也不崩', () => {
    act(() => root.render(<FinancialMetricsCharts metrics={null} income={null} window="all" />))
    expect(host.textContent).toContain('暂无毛利率/净利率数据')
  })

  it('只有 1 期 → 给"不足 2 期"占位而不是画一个点', () => {
    act(() => root.render(
      <FinancialMetricsCharts metrics={[metrics[0]]} income={[income[0]]} window="recent" />,
    ))
    expect(host.textContent).toContain('不足 2 期，无法成线')
    expect(host.textContent).toContain('年报不足 2 期，无法成图')
  })

  it('重复 period_end 被去重, 图上的期数与去重后一致', () => {
    const dup = [...metrics, { ...metrics[2], gross_margin: 0.1 }]
    act(() => root.render(<FinancialMetricsCharts metrics={dup} income={income} window="all" />))
    // scopeNote 里的期数应为 3(去重后), 不是 4
    expect(host.textContent).toContain('共 3 期')
  })

  it('切到「仅年报」时只剩年报期', () => {
    const mixed = [
      ...metrics,
      { period_end: '2025-03-31', announce_date: '2025-04-20', gross_margin: 91, net_margin: 51, revenue_yoy: 5, net_income_yoy: 6 },
    ]
    act(() => root.render(<FinancialMetricsCharts metrics={mixed} income={income} window="annual" />))
    expect(host.textContent).toContain('共 3 期')
    expect(host.textContent).toContain('仅年报')
  })

  it('窗口切换会重设 option(notMerge), 不残留旧序列', () => {
    act(() => root.render(<FinancialMetricsCharts metrics={metrics} income={income} window="recent" />))
    const first = chart.setOption.mock.calls.length
    act(() => root.render(<FinancialMetricsCharts metrics={metrics} income={income} window="annual" />))
    expect(chart.setOption.mock.calls.length).toBeGreaterThan(first)
    const lastCall = chart.setOption.mock.calls[chart.setOption.mock.calls.length - 1]
    expect(lastCall[1]).toBe(true)
  })

  it('卸载时 dispose 掉所有实例', () => {
    act(() => root.render(<FinancialMetricsCharts metrics={metrics} income={income} window="recent" />))
    act(() => root.unmount())
    expect(chart.dispose).toHaveBeenCalledTimes(3)
    root = createRoot(host)   // afterEach 还要 unmount 一次
  })
})

describe('buildSeries 与图表组件的契约', () => {
  it('同比字段全空时不算"有数据"', () => {
    const b = buildSeries(
      [{ period_end: '2024-12-31' }, { period_end: '2025-12-31' }],
      [{ key: 'revenue_yoy', name: '营收同比' }],
    )
    expect(b.series[0].values).toEqual([null, null])
  })
})
