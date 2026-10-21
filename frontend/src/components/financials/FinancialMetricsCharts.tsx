// 财务核心指标图表 — 毛利率/净利率、营收同比/净利同比 两对双折线 + 年报金额图。
//
// 数据处理全部在 @/lib/financialSeries 的纯函数里, 本文件只负责画。
// 所有颜色取自 useChartTheme(轴/网格/tooltip), 序列色用固定语义色:
//   红涨绿跌的语义色只用于"同比"这种有正负方向的线 —— 涨=红(#C74040), 跌=绿(#2D9B65)。
//   毛利率/净利率是水平值不是涨跌, 用蓝/紫区分两条线, 不套红绿。
import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { useChartTheme, type ChartTheme } from '@/lib/theme'
import { fmtBigNum } from '@/lib/format'
import {
  DEFAULT_RECENT_PERIODS,
  amountUnit,
  applyWindow,
  buildAnnualBundle,
  buildSeries,
  dedupeByPeriod,
  isAnnualBundleEmpty,
  isSeriesEmpty,
  sortAscByPeriod,
  toUnit,
  usablePeriodCount,
  windowLabel,
  type AnnualBundle,
  type SeriesBundle,
  type SeriesWindow,
} from '@/lib/financialSeries'

/** 红涨绿跌(A 股语义), 与 K 线图保持同一色值 */
const UP_RED = '#C74040'
const DOWN_GREEN = '#2D9B65'
/** 水平值序列(毛利率/净利率)用的中性色, 不表达涨跌 */
const LEVEL_BLUE = '#3B82F6'
const LEVEL_PURPLE = '#A78BFA'

const AXIS_FONT = 11

interface Props {
  /** 核心指标表原始行(未排序未去重) */
  metrics: Record<string, any>[] | null | undefined
  /** 利润表原始行(年报金额图用) */
  income: Record<string, any>[] | null | undefined
  /** 当前窗口 */
  window: SeriesWindow
  /** 'recent' 窗口的期数 */
  recentN?: number
}

// ================================================================
// option 构造 — 抽成纯函数, 单测可直接断言(不依赖 DOM / echarts 实例)
// ================================================================

function baseGrid() {
  return { left: 52, right: 16, top: 34, bottom: 26, containLabel: false }
}

function axisCommon(ct: ChartTheme) {
  return {
    axisLine: { lineStyle: { color: ct.border } },
    axisTick: { show: false },
    axisLabel: { color: ct.text, fontSize: AXIS_FONT },
    splitLine: { lineStyle: { color: ct.grid } },
  }
}

function tooltipCommon(ct: ChartTheme) {
  return {
    trigger: 'axis' as const,
    backgroundColor: ct.tooltipBg,
    borderColor: ct.tooltipBorder,
    textStyle: { color: ct.tooltipText, fontSize: 11 },
    axisPointer: { type: 'line' as const, lineStyle: { color: ct.crosshair } },
  }
}

function legendCommon(ct: ChartTheme) {
  return { top: 0, right: 0, itemWidth: 10, itemHeight: 2, textStyle: { color: ct.text, fontSize: 11 } }
}

/** 百分比双折线(毛利率/净利率)。数值单位是百分点, 直接带 % 显示。 */
export function buildPercentOption(bundle: SeriesBundle, ct: ChartTheme, colors: string[]): EChartsOption {
  return {
    grid: baseGrid(),
    tooltip: {
      ...tooltipCommon(ct),
      valueFormatter: (v: any) => (v == null ? '—' : `${Number(v).toFixed(2)}%`),
    },
    legend: { ...legendCommon(ct), data: bundle.series.map(s => s.name) },
    xAxis: {
      type: 'category',
      data: bundle.labels,
      boundaryGap: false,
      ...axisCommon(ct),
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      ...axisCommon(ct),
      axisLabel: { color: ct.text, fontSize: AXIS_FONT, formatter: '{value}%' },
    },
    series: bundle.series.map((s, i) => ({
      name: s.name,
      type: 'line',
      data: s.values,
      smooth: false,
      connectNulls: false,    // 缺的一期断开, 不假装连续
      showSymbol: true,
      symbolSize: 4,
      lineStyle: { width: 2, color: colors[i % colors.length] },
      itemStyle: { color: colors[i % colors.length] },
    })),
  }
}

/**
 * 同比双折线(营收同比/净利同比)。
 *
 * 这条线有正负之分, 红涨绿跌要跟着数值走 —— 同一个数据点既可能是红的也可能是绿的,
 * 一条线不能整条一个颜色。echarts 5 的折线做不到按段上色:
 *   - visualMap pieces → 渲染期抛 "Cannot read properties of undefined (reading 'coord')"(5.6.0 实测, 单序列也复现)
 *   - visualMap inRange → 不报错但走的是**连续渐变**, 两个色值之间按数值线性插值,
 *     正负两端都渲染成中间色(实测 20/-5/10/-15 全部落在近绿的灰绿), 等于没有分段
 *   - lineStyle.color 回调 → 只影响数据点, 线本身仍是一条灰线
 * 所以这里改成: 主系列只负责撑起坐标轴和散点(线宽 0), 折线本身用逐段 markLine 画,
 * 每段按该段的数值区间取红/绿。段跨 0 时整段取终点符号对应的颜色, 不做 0 轴截断
 * (截断会在 0 处凭空多出一个拐点, 比颜色略保守更容易误读)。
 */
export function buildYoyOption(bundle: SeriesBundle, ct: ChartTheme): EChartsOption {
  const names = bundle.series.map(s => s.name)
  return {
    grid: baseGrid(),
    tooltip: {
      ...tooltipCommon(ct),
      // 主系列数据就是真实值, tooltip 直接读它; markLine 是 silent 的, 不会干扰
      valueFormatter: (v: any) => (v == null ? '—' : `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(2)}%`),
    },
    legend: { ...legendCommon(ct), data: names },
    xAxis: {
      type: 'category',
      data: bundle.labels,
      boundaryGap: false,
      ...axisCommon(ct),
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      ...axisCommon(ct),
      axisLabel: { color: ct.text, fontSize: AXIS_FONT, formatter: '{value}%' },
    },
    series: bundle.series.map((s, i) => {
      // 营收同比实线+圆点, 净利同比虚线+方点: 两条线共用红/绿语义色, 靠线型和点位形状区分序列
      const solid = i === 0
      return {
        name: s.name,
        type: 'line' as const,
        data: s.values,
        connectNulls: false,
        lineStyle: { width: 0 },              // 线由 markLine 画, 这里隐藏
        showSymbol: true,
        symbol: solid ? 'circle' : 'rect',
        symbolSize: 5,
        itemStyle: { color: (p: any) => colorOf(p.value) },
        markLine: {
          silent: true,
          symbol: ['none', 'none'],
          animation: false,
          label: { show: false },
          data: segmentLines(bundle.labels, s.values, solid),
        },
      }
    }),
  }
}

/** 按数值正负取红/绿; 0 与缺失都算中性, 用二级文字色(0 既不是涨也不是跌)。 */
export function colorOf(v: unknown): string {
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n) || n === 0) return '#71717A'
  return n > 0 ? UP_RED : DOWN_GREEN
}

/**
 * 把一条序列拆成逐段 markLine 配置。
 *
 * 每段 = 相邻两个非空点。段的颜色按"这一段的值域"决定: 两端同号用该号的颜色;
 * 跨 0 时取终点的符号(即"这一期相对上一期是正还是负"), 不做 0 轴截断。
 * 中间隔着 null 的两点不连(与 connectNulls:false 一致)。
 */
export function segmentLines(labels: string[], values: (number | null)[], solid: boolean) {
  const out: any[] = []
  let prev: { label: string; value: number } | null = null
  for (let i = 0; i < values.length; i++) {
    const v = values[i]
    if (v == null || !Number.isFinite(v)) {
      prev = null          // 断点: 不跨 null 连线
      continue
    }
    if (prev) {
      const [a, b] = [prev.value, v]
      // 同号取该号色; 跨 0 取终点色
      const endsUp = a >= 0 && b >= 0
      const endsDown = a <= 0 && b <= 0
      const color = endsUp ? UP_RED : endsDown ? DOWN_GREEN : b > 0 ? UP_RED : DOWN_GREEN
      out.push([
        { coord: [prev.label, prev.value], lineStyle: { color, width: solid ? 2 : 1.8, type: solid ? 'solid' : 'dashed' } },
        { coord: [labels[i], v], lineStyle: { color, width: solid ? 2 : 1.8, type: solid ? 'solid' : 'dashed' } },
      ])
    }
    prev = { label: labels[i], value: v }
  }
  return out
}

/**
 * 年报金额图: 基本 EPS 折线 + 营收/归母净利柱状(双 Y 轴)。
 *
 * 金额走右轴, EPS 走左轴 —— 两者量级差 8~10 个数量级, 挤一根轴会把 EPS 压成直线。
 * 金额按整根轴的最大绝对值统一换算成 亿/万, 与 fmtBigNum 口径一致。
 */
export function buildAnnualOption(b: AnnualBundle, ct: ChartTheme): EChartsOption {
  const unit = amountUnit([...b.revenue, ...b.netIncome])
  const rev = b.revenue.map(v => toUnit(v, unit))
  const ni = b.netIncome.map(v => toUnit(v, unit))
  const unitText = unit.suffix ? `（${unit.suffix}元）` : '（元）'

  return {
    grid: { ...baseGrid(), top: 26, right: 62, bottom: 46 },
    tooltip: {
      ...tooltipCommon(ct),
      formatter: (params: any) => {
        const arr = Array.isArray(params) ? params : [params]
        if (arr.length === 0) return ''
        const idx = arr[0].dataIndex
        const lines = [`<div style="font-size:11px;color:${ct.textStrong};margin-bottom:2px">${b.periods[idx]}</div>`]
        const epsV = b.eps[idx]
        lines.push(`基本每股收益　${epsV == null ? '—' : `${epsV.toFixed(2)} 元`}`)
        lines.push(`营业收入　　　${fmtBigNum(b.revenue[idx])}`)
        lines.push(`归母净利润　　${fmtBigNum(b.netIncome[idx])}`)
        return lines.join('<br/>')
      },
    },
    // 图例放到底部: 三个条目比较宽, 放右上会和左轴名「元/股」/右轴名撞在一起
    legend: {
      bottom: 0, left: 'center', itemWidth: 10, itemHeight: 8,
      textStyle: { color: ct.text, fontSize: 11 },
      data: ['营业收入', '归母净利润', '基本每股收益'],
    },
    xAxis: {
      type: 'category',
      data: b.labels,
      ...axisCommon(ct),
      splitLine: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        name: '元/股',
        nameLocation: 'end',
        nameGap: 8,
        nameTextStyle: { color: ct.text, fontSize: 10, align: 'left' },
        ...axisCommon(ct),
        axisLabel: { color: ct.text, fontSize: AXIS_FONT },
      },
      {
        type: 'value',
        name: unitText,
        nameLocation: 'end',
        nameGap: 8,
        nameTextStyle: { color: ct.text, fontSize: 10, align: 'right' },
        ...axisCommon(ct),
        splitLine: { show: false },
        axisLabel: { color: ct.text, fontSize: AXIS_FONT },
      },
    ],
    series: [
      {
        name: '营业收入',
        type: 'bar',
        yAxisIndex: 1,
        data: rev,
        barMaxWidth: 26,
        itemStyle: { color: LEVEL_BLUE, opacity: 0.85 },
      },
      {
        name: '归母净利润',
        type: 'bar',
        yAxisIndex: 1,
        data: ni,
        barMaxWidth: 26,
        itemStyle: { color: LEVEL_PURPLE, opacity: 0.85 },
      },
      {
        name: '基本每股收益',
        type: 'line',
        yAxisIndex: 0,
        data: b.eps,
        smooth: false,
        connectNulls: false,
        showSymbol: true,
        symbolSize: 5,
        lineStyle: { width: 2, color: '#F59E0B' },
        itemStyle: { color: '#F59E0B' },
      },
    ],
  }
}

// ================================================================
// 组件
// ================================================================

/** 极简 echarts 宿主: init / setOption(true) / ResizeObserver / dispose。 */
function EChartHost({ option, height }: { option: EChartsOption; height: number }) {
  const elRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    const el = elRef.current
    if (!el) return
    const chart = echarts.init(el, undefined, { renderer: 'canvas' })
    chartRef.current = chart
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(el)
    return () => {
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    // notMerge=true: 窗口/标的切换时序列条数会变, 不合并可避免残留旧序列
    chartRef.current?.setOption(option, true)
  }, [option])

  return <div ref={elRef} style={{ width: '100%', height }} />
}

function EmptyHint({ text }: { text: string }) {
  return (
    <div className="flex items-center justify-center rounded-btn border border-dashed border-border/60 text-[11px] text-muted" style={{ height: 150 }}>
      {text}
    </div>
  )
}

/** 一小节: 标题 + 右上角口径说明 + 图或占位。 */
function ChartBlock({
  title,
  note,
  empty,
  emptyText,
  height,
  option,
}: {
  title: string
  note?: string
  empty: boolean
  emptyText: string
  height: number
  option: EChartsOption | null
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 mb-1.5">
        <span className="text-xs font-medium text-secondary">{title}</span>
        {note && <span className="text-[10px] text-muted truncate">{note}</span>}
      </div>
      {empty || !option ? <EmptyHint text={emptyText} /> : <EChartHost option={option} height={height} />}
    </div>
  )
}

export function FinancialMetricsCharts({ metrics, income, window: win, recentN = DEFAULT_RECENT_PERIODS }: Props) {
  const ct = useChartTheme()

  // 去重 → 升序 → 按窗口切片。整条链路只跑一次, 三个图共用同一批行。
  const rowsAsc = useMemo(
    () => applyWindow(sortAscByPeriod(dedupeByPeriod(metrics)), win, recentN),
    [metrics, win, recentN],
  )

  // 年报金额图不受窗口切换影响(它天生只画年报), 但要去重排序
  const annual = useMemo(
    () => buildAnnualBundle(
      sortAscByPeriod(dedupeByPeriod(metrics)),
      sortAscByPeriod(dedupeByPeriod(income)),
    ),
    [metrics, income],
  )

  const marginBundle = useMemo(
    () => buildSeries(rowsAsc, [
      { key: 'gross_margin', name: '毛利率' },
      { key: 'net_margin', name: '净利率' },
    ]),
    [rowsAsc],
  )

  const yoyBundle = useMemo(
    () => buildSeries(rowsAsc, [
      { key: 'revenue_yoy', name: '营收同比' },
      { key: 'net_income_yoy', name: '净利同比' },
    ]),
    [rowsAsc],
  )

  const marginEmpty = isSeriesEmpty(marginBundle) || usablePeriodCount(marginBundle) < 2
  const yoyEmpty = isSeriesEmpty(yoyBundle) || usablePeriodCount(yoyBundle) < 2
  const annualEmpty = isAnnualBundleEmpty(annual) || annual.periods.length < 2

  const marginOption = useMemo(
    () => (marginEmpty ? null : buildPercentOption(marginBundle, ct, [LEVEL_BLUE, LEVEL_PURPLE])),
    [marginEmpty, marginBundle, ct],
  )
  const yoyOption = useMemo(
    () => (yoyEmpty ? null : buildYoyOption(yoyBundle, ct)),
    [yoyEmpty, yoyBundle, ct],
  )
  const annualOption = useMemo(
    () => (annualEmpty ? null : buildAnnualOption(annual, ct)),
    [annualEmpty, annual, ct],
  )

  const scopeNote = `口径：${windowLabel(win, recentN)}，共 ${rowsAsc.length} 期`

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <ChartBlock
          title="毛利率 / 净利率"
          note={scopeNote}
          empty={marginEmpty}
          emptyText={usablePeriodCount(marginBundle) === 0 ? '暂无毛利率/净利率数据' : '不足 2 期，无法成线'}
          height={170}
          option={marginOption}
        />
        <ChartBlock
          title="营收同比 / 净利同比"
          note={scopeNote}
          empty={yoyEmpty}
          emptyText={usablePeriodCount(yoyBundle) === 0 ? '暂无同比数据' : '不足 2 期，无法成线'}
          height={170}
          option={yoyOption}
        />
      </div>

      <ChartBlock
        title="年报金额：营业收入 / 归母净利润 / 基本每股收益"
        note="仅年报（12-31）· 柱=金额（右轴），线=EPS（左轴）"
        empty={annualEmpty}
        emptyText={annual.periods.length === 0 ? '暂无年报数据' : '年报不足 2 期，无法成图'}
        height={210}
        option={annualOption}
      />
    </div>
  )
}
