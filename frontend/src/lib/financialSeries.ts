// 财务图表的纯数据处理 — 不依赖 React / ECharts, 便于单测。
//
// 数据形状与后端 /api/financials/* 一致: 每行一期报告, period_end 为 'YYYY-MM-DD'。
// 后端默认 latest_only, 只返回最新一期; 要画多期折线, 调用方需显式请求多期
// (见 useFinancialSeries 的 periods 参数)。本模块只管"拿到一堆期之后怎么摆",
// 不关心数据从哪来。

import type { FinancialIncomeRecord, FinancialMetricRecord } from '@/lib/api'

/** 图表展示窗长档位。'recent' = 近 N 期(默认); 'all' = 全部; 'annual' = 仅年报。 */
export type SeriesWindow = 'recent' | 'all' | 'annual'

/** 默认"近 N 期"的 N。12 期 = 3 个完整年度, 足以看清趋势又不至于挤成毛线。 */
export const DEFAULT_RECENT_PERIODS = 12

/** 年报 period_end 判定: A 股年报一律 12-31, 季报/半年报是 03-31/06-30/09-30。 */
export const ANNUAL_PERIOD_RE = /-12-31$/

/** 单期数据。数值字段统一 null 表示缺失, 不参与连线(gap)。 */
export interface PeriodPoint {
  /** 报告期 YYYY-MM-DD */
  period: string
  /** x 轴短标签。季报含年份, 年报只显示年份 */
  label: string
  /** 该期公告日 YYYY-MM-DD, 可能为 null */
  announceDate: string | null
  /** 是否年报 */
  isAnnual: boolean
  /** 指标值(百分点 / 元 / 元每股), 缺失为 null */
  value: number | null
}

/** 一条序列: 名称 + 各期取值。values 与 labels 等长且同序。 */
export interface SeriesBundle {
  labels: string[]
  periods: string[]
  isAnnual: boolean[]
  series: { name: string; values: (number | null)[] }[]
}

/** 年报图的完整数据包 */
export interface AnnualBundle {
  labels: string[]
  periods: string[]
  /** 基本 EPS 折线, 元/股 */
  eps: (number | null)[]
  /** 营业收入, 原始单位元 */
  revenue: (number | null)[]
  /** 归母净利润, 原始单位元 */
  netIncome: (number | null)[]
}

/**
 * 通用的"一行一期"形状。
 *
 * 不直接写成 FinancialMetricRecord | FinancialIncomeRecord —— 那样外部传进来的
 * Record<string, any>[] 反而赋不进去(联合类型要求先满足其中一个), 而图表只关心
 * period_end / announce_date 两个字段, 其余按 key 动态取。两个 Record 类型仍在
 * buildAnnualBundle 的入参上保留, 以表达"这里期望的是指标表/利润表"。
 */
export type Row = Record<string, any>

function num(v: unknown): number | null {
  // 后端可能给 null / undefined / '' / '--', 也可能是字符串数字
  if (v == null || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

function str(v: unknown): string {
  return typeof v === 'string' ? v.trim() : v == null ? '' : String(v).trim()
}

/**
 * 去重: 同一 period_end 只保留一条。
 *
 * 保留规则 — 优先 announce_date 更新的一条(财报更正/重述后以新公告为准);
 * announce_date 相同或都缺失时, 保留数组里更靠后的一条(后端按同步顺序追加,
 * 后写入的是较新的落库结果)。绝不按数值大小挑, 避免"取好的那个"这种隐性择优。
 */
export function dedupeByPeriod<T extends Row>(rows: T[] | null | undefined): T[] {
  const byPeriod = new Map<string, T>()
  for (const row of rows ?? []) {
    const period = str(row?.period_end)
    if (!period) continue          // 没有报告期的行无法定位, 直接丢弃
    const prev = byPeriod.get(period)
    if (!prev) {
      byPeriod.set(period, row)
      continue
    }
    const prevAnn = str(prev.announce_date)
    const nextAnn = str(row.announce_date)
    // 更大的 announce_date 字符串 = 更晚公告(ISO 日期可直接比较); 相等则用后者
    if (nextAnn >= prevAnn) byPeriod.set(period, row)
  }
  return Array.from(byPeriod.values())
}

/** 升序时间轴(左旧右新)。ISO 日期字符串可直接字典序比较。 */
export function sortAscByPeriod<T extends Row>(rows: T[]): T[] {
  return rows.slice().sort((a, b) => str(a.period_end).localeCompare(str(b.period_end)))
}

/** 是否为年报报告期。 */
export function isAnnualPeriod(period: string): boolean {
  return ANNUAL_PERIOD_RE.test(period)
}

/**
 * x 轴标签。年报只写年份('2024'), 其余期写 'YYYY-MM'(季报)或 'YYYY-MM-DD' 都不必要,
 * 保留年月即可唯一区分。
 */
export function periodLabel(period: string): string {
  if (isAnnualPeriod(period)) return period.slice(0, 4)
  return period.slice(0, 7)
}

/**
 * 生成一组不会重复的 x 轴短标签。
 *
 * 单看一期, 'YYYY'(年报) 和 'YYYY-MM'(季报) 都好读; 但混在一个轴上, 同一年里
 * 既出现 '2022' 又出现 '2022-03', 读起来就像第 2 期也叫 2022。所以同期出现年报时
 * 统一退化成 'YY-MM'(24-12 / 24-03), 纯年报或年内无年报时保持原来的写法。
 */
export function axisLabels(periods: string[]): string[] {
  const byYear = new Map<string, { annual: boolean; quarter: boolean }>()
  for (const p of periods) {
    const y = p.slice(0, 4)
    const cur = byYear.get(y) ?? { annual: false, quarter: false }
    if (isAnnualPeriod(p)) cur.annual = true
    else cur.quarter = true
    byYear.set(y, cur)
  }
  return periods.map(p => {
    const y = p.slice(0, 4)
    const g = byYear.get(y)!
    // 同一年里年报和季报并存 → 退化成 'YY-MM' 消除歧义
    if (g.annual && g.quarter) return `${y.slice(2)}-${p.slice(5, 7)}`
    return periodLabel(p)
  })
}

/** 窗口过滤。'recent' 取升序后最后 n 期, 'annual' 只留 12-31, 'all' 原样。 */export function applyWindow<T extends Row>(
  rowsAsc: T[],
  win: SeriesWindow,
  recentN: number = DEFAULT_RECENT_PERIODS,
): T[] {
  if (win === 'annual') return rowsAsc.filter(r => isAnnualPeriod(str(r.period_end)))
  if (win === 'recent') {
    const n = Number.isFinite(recentN) && recentN > 0 ? Math.floor(recentN) : DEFAULT_RECENT_PERIODS
    return rowsAsc.slice(Math.max(0, rowsAsc.length - n))
  }
  return rowsAsc
}

/**
 * 组装一张双折线图的数据。
 *
 * fields 里的 key 对不上(全 null)不算错误 — 上层据此决定是否画图还是给占位文案。
 * values 保留 null 而不是填 0: 填 0 会在图上画出一条假的下探, 比断线更误导。
 */
export function buildSeries(
  rowsAsc: Row[],
  fields: { key: string; name: string }[],
): SeriesBundle {
  const periods = rowsAsc.map(r => str(r.period_end))
  const labels = axisLabels(periods)
  const isAnnual = periods.map(isAnnualPeriod)
  const series = fields.map(f => ({
    name: f.name,
    values: rowsAsc.map(r => num((r as Record<string, any>)[f.key])),
  }))
  return { labels, periods, isAnnual, series }
}

/** 序列是否全为空(null)。全空时图上没东西可画, 上层显示占位。 */
export function isSeriesEmpty(bundle: SeriesBundle | null | undefined): boolean {
  if (!bundle || bundle.series.length === 0) return true
  return bundle.series.every(s => s.values.every(v => v == null))
}

/** 有非空值的期数(任一条序列)。用来判断"少于 2 期"是否该给占位。 */
export function usablePeriodCount(bundle: SeriesBundle | null | undefined): number {
  if (!bundle) return 0
  let count = 0
  for (let i = 0; i < bundle.labels.length; i++) {
    if (bundle.series.some(s => s.values[i] != null)) count++
  }
  return count
}

/**
 * 基本 EPS 取数: 优先 metrics.eps_basic, 缺失时回落到 income.basic_eps。
 *
 * 两表口径一致(都是基本每股收益), 但覆盖不全 — 有的期 metrics 有值、有的期只有
 * income 有值。按 period 对齐后逐期回落, 而不是"整条序列二选一", 否则会白白丢掉
 * 半条线。
 */
export function mergeEps(
  metrics: FinancialMetricRecord[] | Row[] | null | undefined,
  income: FinancialIncomeRecord[] | Row[] | null | undefined,
): Map<string, number | null> {
  const out = new Map<string, number | null>()
  for (const r of (income ?? []) as Row[]) {
    const p = str(r?.period_end)
    if (p) out.set(p, num(r.basic_eps))
  }
  // metrics 优先级更高, 后写覆盖前写
  for (const r of (metrics ?? []) as Row[]) {
    const p = str(r?.period_end)
    if (!p) continue
    const v = num(r.eps_basic)
    if (v != null) out.set(p, v)
    else if (!out.has(p)) out.set(p, null)
  }
  return out
}

/**
 * 年报金额图数据包。
 *
 * 年报对齐以"年报报告期"为骨架 — 只要营收/归母净利/基本 EPS 任一有值就保留这一年,
 * 避免某一字段缺失导致整年消失。三者都空则该年不出现。
 */
export function buildAnnualBundle(
  metricsAsc: FinancialMetricRecord[] | Row[],
  incomeAsc: FinancialIncomeRecord[] | Row[],
): AnnualBundle {
  const epsMap = mergeEps(metricsAsc as Row[], incomeAsc as Row[])

  const incByPeriod = new Map<string, Row>()
  for (const r of incomeAsc as Row[]) {
    const p = str(r?.period_end)
    if (p) incByPeriod.set(p, r)
  }

  // 收集所有出现过的年报期(来自三处), 去重后升序
  const periodSet = new Set<string>()
  for (const p of epsMap.keys()) if (isAnnualPeriod(p)) periodSet.add(p)
  for (const p of incByPeriod.keys()) if (isAnnualPeriod(p)) periodSet.add(p)
  const periods = Array.from(periodSet).sort((a, b) => a.localeCompare(b))

  const kept: string[] = []
  const eps: (number | null)[] = []
  const revenue: (number | null)[] = []
  const netIncome: (number | null)[] = []
  for (const p of periods) {
    const inc = incByPeriod.get(p)
    const e = epsMap.get(p) ?? null
    const rev = num(inc?.revenue)
    const ni = num(inc?.net_income_attributable)
    if (e == null && rev == null && ni == null) continue
    kept.push(p)
    eps.push(e)
    revenue.push(rev)
    netIncome.push(ni)
  }

  return {
    labels: kept.map(p => p.slice(0, 4)),
    periods: kept,
    eps,
    revenue,
    netIncome,
  }
}

/** 年报图是否完全没数据可画。 */
export function isAnnualBundleEmpty(b: AnnualBundle | null | undefined): boolean {
  if (!b || b.periods.length === 0) return true
  return b.eps.every(v => v == null)
    && b.revenue.every(v => v == null)
    && b.netIncome.every(v => v == null)
}

/**
 * 金额轴单位: 元 → 亿 / 万。
 *
 * 与 fmtBigNum 的口径一致(≥1e8 用亿, ≥1e4 用万), 但这里是给整根坐标轴定一个
 * 统一除数, 必须按序列里的最大绝对值来定, 不能逐点换算 — 否则同一根轴上
 * 一会儿亿一会儿万, 读数会读错。
 */
export function amountUnit(values: (number | null)[]): { div: number; suffix: string; digits: number } {
  let maxAbs = 0
  for (const v of values) {
    if (v == null) continue
    const a = Math.abs(v)
    if (a > maxAbs) maxAbs = a
  }
  if (maxAbs >= 1_000_000_000_000) return { div: 1_000_000_000_000, suffix: '万亿', digits: 2 }
  if (maxAbs >= 100_000_000) return { div: 100_000_000, suffix: '亿', digits: 2 }
  if (maxAbs >= 10_000) return { div: 10_000, suffix: '万', digits: 0 }
  return { div: 1, suffix: '', digits: 0 }
}

/** 按轴上定好的单位换算单个金额; null 保持 null(断线)。 */
export function toUnit(v: number | null, unit: { div: number }): number | null {
  if (v == null) return null
  return v / unit.div
}

/** 窗口切换项的中文名。 */
export function windowLabel(win: SeriesWindow, recentN: number = DEFAULT_RECENT_PERIODS): string {
  if (win === 'annual') return '仅年报'
  if (win === 'all') return '全部'
  return `近 ${recentN} 期`
}
