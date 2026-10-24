/**
 * 股票列表单元格渲染原语（共享）。
 *
 * 只负责「无业务上下文」的纯数据列：价格/成交/均线/区间/技术指标/动量/连板/财务等。
 * symbol、strategies、score、signals、candle 等需要页面上下文（加自选按钮、失效行、
 * kline 数据、信号提取）的列由各页面的 renderCell 自行处理。
 *
 * 口径与原 ScreenerTable 对齐（已和自选页校准过）：amplitude 用 *100、annual_vol/
 * 财务率类用 fmtPct、kdj 用 toFixed(1)、vol_ma 用 fmtBigNum 等。
 */
import type { ReactNode } from 'react'
import { fmtPrice, fmtPct, fmtBigNum, fmtVolume, priceColorClass, cnDateFromUtc } from '@/lib/format'
import type { ColumnConfig } from '@/lib/list-columns'
import { NUM_CELL_CLASS } from '@/lib/stock-table'
import { isChiNext, isStar } from '@/lib/board'

// ===== 板块标识（自选/策略页统一口径） =====

export function boardTag(symbol: string): { label: string; color: string } | null {
  if (isChiNext(symbol))         return { label: '创', color: 'text-[#f97316] bg-[#f97316]/12 border-[#f97316]/25' }
  if (isStar(symbol))            return { label: '科', color: 'text-cyan-400 bg-cyan-400/12 border-cyan-400/25' }
  if (/\.BJ$/.test(symbol))      return { label: '北', color: 'text-purple-400 bg-purple-400/12 border-purple-400/25' }
  return null
}

// ===== RSI 指标带颜色 =====

export function RSIBadge({ value }: { value: number | null | undefined }) {
  if (value == null || !Number.isFinite(value)) return <span className="text-muted">—</span>
  let color = 'text-secondary'
  if (value >= 80) color = 'text-danger font-medium'
  else if (value >= 70) color = 'text-bull'
  else if (value <= 20) color = 'text-bear font-medium'
  else if (value <= 30) color = 'text-bear'
  return <span className={`tabular-nums ${color}`}>{value.toFixed(1)}</span>
}

// ===== 纯数据列渲染 =====

/** 数值 → 固定小数位文本; 非有限数 / 缺失一律 —。
 *
 * 裸 `x.toFixed(1)` 对 Infinity 返回 `"Infinity"`、对 NaN 返回 `"NaN"`, 直接上屏。
 * 这类列 (kdj / turnover 等) 不像 price 那样经过 fmtPrice, 必须单独守。 */
function fmtFixed(value: unknown, digits: number, suffix = ''): string {
  const n = Number(value)
  if (value == null || value === '' || !Number.isFinite(n)) return '—'
  return `${n.toFixed(digits)}${suffix}`
}

function fmtMaybePrice(value: any) {
  return value != null && Number.isFinite(value) ? fmtPrice(value) : '—'
}

export type SelectionCellGaps = { gapKeys?: string[] }

function isBlankCell(value: unknown): boolean {
  if (value == null) return true
  if (typeof value === 'number' && !Number.isFinite(value)) return true
  if (typeof value === 'string' && value.trim() === '') return true
  return false
}

/** 百分数原样显示：2.35 表示 2.35%，不再乘 100。 */
function fmtPercentPoint(value: number): string {
  return `${value.toFixed(2)}%`
}

/** 0–1 比率显示成百分数，不加正负号。空值是「—」。 */
function fmtUnsignedRatio(value: unknown): string {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(2)}%`
}

function gapTd(col: ColumnConfig): ReactNode {
  return <td key={col.id} className={`${alignTdClass(col.align)} text-warning`}>缺数据</td>
}

function streakBadge(col: ColumnConfig, value: number, tone: 'up' | 'down'): ReactNode {
  if (!(value > 0)) return <td key={col.id} className="px-3 py-2 text-center"><span className="text-muted">—</span></td>
  const color = tone === 'up' ? 'bg-danger/15 text-danger' : 'bg-bear/15 text-bear'
  return (
    <td key={col.id} className="px-3 py-2 text-center">
      <span className={`inline-flex items-center justify-center min-w-[20px] h-5 px-1 rounded text-xs font-bold tabular-nums ${color}`}>
        {value}
      </span>
    </td>
  )
}

/**
 * 渲染一个纯数据内置列的 <td>。
 * 返回 null 表示该列不属于纯数据列（symbol/strategies/score/signals/candle 等），由调用方处理。
 */
export function renderBuiltinDataCell(
  r: any,
  col: ColumnConfig,
  gaps?: SelectionCellGaps,
): ReactNode | null {
  if (col.source.type !== 'builtin') return null
  const key = col.source.key

  // 这些列需要业务上下文，不在此处理
  if (key === 'symbol' || key === 'strategies' || key === 'score' || key === 'signals' || key === 'candle') {
    return null
  }

  const numCls = `${alignTdClass(col.align)} ${NUM_CELL_CLASS}`

  switch (key) {
    // 价格
    case 'price':
      return <td key={col.id} className={`${numCls} text-secondary`}>{fmtMaybePrice(r.close)}</td>
    case 'pct':
      return <td key={col.id} className={`${numCls} font-medium ${priceColorClass(r.change_pct)}`}>{fmtPct(r.change_pct)}</td>
    case 'change_amount':
      return <td key={col.id} className={`${numCls} ${priceColorClass(r.change_amount)}`}>{r.change_amount != null ? fmtPrice(r.change_amount) : '—'}</td>
    case 'amplitude':
      return <td key={col.id} className={numCls}>{r.amplitude != null && Number.isFinite(Number(r.amplitude)) ? `${(Number(r.amplitude) * 100).toFixed(2)}%` : '—'}</td>
    case 'open':         return <td key={col.id} className={numCls}>{fmtMaybePrice(r.open)}</td>
    case 'high':         return <td key={col.id} className={numCls}>{fmtMaybePrice(r.high)}</td>
    case 'low':          return <td key={col.id} className={numCls}>{fmtMaybePrice(r.low)}</td>
    case 'prev_close':   return <td key={col.id} className={numCls}>{fmtMaybePrice(r.prev_close)}</td>
    // 涨跌停价 (仅股票有值, ETF/指数后端置 null); A 股口径红涨绿跌
    case 'limit_up_price':
      return <td key={col.id} className={`${numCls} ${r.limit_up_price != null ? 'text-danger' : ''}`}>{r.limit_up_price != null ? fmtPrice(r.limit_up_price) : '—'}</td>
    case 'limit_down_price':
      return <td key={col.id} className={`${numCls} ${r.limit_down_price != null ? 'text-bear' : ''}`}>{r.limit_down_price != null ? fmtPrice(r.limit_down_price) : '—'}</td>
    // 成交
    case 'turnover':
      return <td key={col.id} className={numCls}>{fmtFixed(r.turnover_rate, 2, '%')}</td>
    case 'volume':
      return <td key={col.id} className={`${numCls} text-secondary`}>{r.volume != null ? fmtVolume(r.volume) : '—'}</td>
    case 'amount':
      return <td key={col.id} className={`${numCls} text-secondary`}>{fmtBigNum(r.amount)}</td>
    case 'float_val':
      return <td key={col.id} className={`${numCls} text-secondary`}>{r.float_shares && r.close ? fmtBigNum(r.float_shares * r.close) : '—'}</td>
    case 'vol_ratio':
      return (
        <td key={col.id} className={numCls}>
          <span className={r.vol_ratio_5d >= 2 ? 'text-accent font-medium' : ''}>
            {fmtMaybePrice(r.vol_ratio_5d)}
          </span>
        </td>
      )
    case 'annual_vol':
      return <td key={col.id} className={numCls}>{r.annual_vol_20d != null ? fmtPct(r.annual_vol_20d) : '—'}</td>
    // 均线
    case 'ma5':  return <td key={col.id} className={numCls}>{fmtMaybePrice(r.ma5)}</td>
    case 'ma10': return <td key={col.id} className={numCls}>{fmtMaybePrice(r.ma10)}</td>
    case 'ma20': return <td key={col.id} className={numCls}>{fmtMaybePrice(r.ma20)}</td>
    case 'ma60': return <td key={col.id} className={numCls}>{fmtMaybePrice(r.ma60)}</td>
    // 区间
    case 'high_60d': return <td key={col.id} className={numCls}>{r.high_60d != null ? fmtPrice(r.high_60d) : '—'}</td>
    case 'low_60d':  return <td key={col.id} className={numCls}>{r.low_60d != null ? fmtPrice(r.low_60d) : '—'}</td>
    // 技术指标
    case 'rsi6':  return <td key={col.id} className={numCls}><RSIBadge value={r.rsi_6} /></td>
    case 'rsi14': return <td key={col.id} className={numCls}><RSIBadge value={r.rsi_14} /></td>
    case 'rsi24': return <td key={col.id} className={numCls}><RSIBadge value={r.rsi_24} /></td>
    case 'macd_dif':
      return <td key={col.id} className={`${numCls} ${priceColorClass(r.macd_hist)}`}>{r.macd_dif != null ? fmtPrice(r.macd_dif) : '—'}</td>
    case 'macd_dea':
      return <td key={col.id} className={numCls}>{r.macd_dea != null ? fmtPrice(r.macd_dea) : '—'}</td>
    case 'macd_hist':
      return <td key={col.id} className={`${numCls} ${priceColorClass(r.macd_hist)}`}>{r.macd_hist != null ? fmtPrice(r.macd_hist) : '—'}</td>
    case 'kdj_k': return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_k, 1)}</td>
    case 'kdj_d': return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_d, 1)}</td>
    case 'kdj_j': return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_j, 1)}</td>
    case 'boll_upper': return <td key={col.id} className={numCls}>{r.boll_upper != null ? fmtPrice(r.boll_upper) : '—'}</td>
    case 'boll_lower': return <td key={col.id} className={numCls}>{r.boll_lower != null ? fmtPrice(r.boll_lower) : '—'}</td>
    case 'atr14':    return <td key={col.id} className={numCls}>{r.atr_14 != null ? fmtPrice(r.atr_14) : '—'}</td>
    case 'vol_ma5':  return <td key={col.id} className={numCls}>{r.vol_ma5 != null ? fmtBigNum(r.vol_ma5) : '—'}</td>
    case 'vol_ma10': return <td key={col.id} className={numCls}>{r.vol_ma10 != null ? fmtBigNum(r.vol_ma10) : '—'}</td>
    // 动量
    case 'momentum_5d':  return <td key={col.id} className={`${numCls} ${priceColorClass(r.momentum_5d)}`}>{fmtPct(r.momentum_5d)}</td>
    case 'momentum_10d': return <td key={col.id} className={`${numCls} ${priceColorClass(r.momentum_10d)}`}>{fmtPct(r.momentum_10d)}</td>
    case 'momentum_20d': return <td key={col.id} className={`${numCls} ${priceColorClass(r.momentum_20d)}`}>{fmtPct(r.momentum_20d)}</td>
    case 'momentum_30d': return <td key={col.id} className={`${numCls} ${priceColorClass(r.momentum_30d)}`}>{fmtPct(r.momentum_30d)}</td>
    case 'momentum_60d': return <td key={col.id} className={`${numCls} ${priceColorClass(r.momentum_60d)}`}>{fmtPct(r.momentum_60d)}</td>
    case 'deviate_3d':  return <td key={col.id} className={`${numCls} ${priceColorClass(r.deviate_3d)}`} title="偏离值 = 个股3日涨跌幅 − 对应指数 (主板±20%/创业科创±30%/北交所±40% 触发)">{fmtPct(r.deviate_3d)}</td>
    case 'deviate_10d': return <td key={col.id} className={`${numCls} ${priceColorClass(r.deviate_10d)}`} title="10日累计偏离 (+100% 触发严重异常波动)">{fmtPct(r.deviate_10d)}</td>
    case 'deviate_30d': return <td key={col.id} className={`${numCls} ${priceColorClass(r.deviate_30d)}`} title="30日累计偏离 (+200% 触发严重异常波动)">{fmtPct(r.deviate_30d)}</td>
    // 连板
    case 'limit_ups':
      return (
        <td key={col.id} className="px-3 py-2 text-center">
          {r.consecutive_limit_ups > 0 ? (
            <span className="inline-flex items-center justify-center min-w-[20px] h-5 px-1 rounded bg-danger/15 text-danger text-xs font-bold tabular-nums">
              {r.consecutive_limit_ups}
            </span>
          ) : (
            <span className="text-muted">—</span>
          )}
        </td>
      )
    case 'limit_downs':
      return (
        <td key={col.id} className="px-3 py-2 text-center">
          {r.consecutive_limit_downs > 0 ? (
            <span className="inline-flex items-center justify-center min-w-[20px] h-5 px-1 rounded bg-bear/15 text-bear text-xs font-bold tabular-nums">
              {r.consecutive_limit_downs}
            </span>
          ) : (
            <span className="text-muted">—</span>
          )}
        </td>
      )
    // 自选加入信息（后端读时计算；added_at 为 naive UTC，须转北京时间展示）
    case 'added_at':
      return (
        <td key={col.id} className={`${alignTdClass(col.align)} text-secondary`}>
          {cnDateFromUtc(r.added_at) || '—'}
        </td>
      )
    case 'pct_since_added':
      return <td key={col.id} className={`${numCls} ${priceColorClass(r.pct_since_added)}`}>{fmtPct(r.pct_since_added)}</td>
    // 财务指标（后端 enriched 未返回时显示 —）
    case 'eps':           return <td key={col.id} className={numCls}>{r.eps != null ? fmtPrice(r.eps) : '—'}</td>
    case 'bps':           return <td key={col.id} className={numCls}>{r.bps != null ? fmtPrice(r.bps) : '—'}</td>
    case 'roe':           return <td key={col.id} className={numCls}>{r.roe != null ? fmtPct(r.roe) : '—'}</td>
    case 'pe_ttm':
      if (isBlankCell(r.pe_ttm) && gaps?.gapKeys?.includes('pe_ttm')) return gapTd(col)
      return <td key={col.id} className={numCls}>{r.pe_ttm != null ? fmtPrice(r.pe_ttm) : '—'}</td>
    case 'northbound_hold_pct':
      if (isBlankCell(r.northbound_hold_pct) && gaps?.gapKeys?.includes('northbound_hold_pct')) return gapTd(col)
      return <td key={col.id} className={numCls}>{r.northbound_hold_pct != null ? fmtPercentPoint(Number(r.northbound_hold_pct)) : '—'}</td>
    case 'holder_num':
      if (isBlankCell(r.holder_num) && gaps?.gapKeys?.includes('holder_num')) return gapTd(col)
      return <td key={col.id} className={numCls}>{r.holder_num != null ? Math.round(Number(r.holder_num)).toLocaleString('zh-CN') : '—'}</td>
    case 'holder_change_pct':
      if (isBlankCell(r.holder_change_pct) && gaps?.gapKeys?.includes('holder_change_pct')) return gapTd(col)
      return <td key={col.id} className={numCls}>{r.holder_change_pct != null ? fmtPercentPoint(Number(r.holder_change_pct)) : '—'}</td>
    case 'index_membership':
      if (isBlankCell(r.index_membership) && gaps?.gapKeys?.includes('index_membership')) return gapTd(col)
      return <td key={col.id} className={`${alignTdClass(col.align)} text-secondary`}>{r.index_membership || '—'}</td>
    case 'next_unlock_date':
      if (isBlankCell(r.next_unlock_date) && gaps?.gapKeys?.includes('next_unlock_date')) return gapTd(col)
      return <td key={col.id} className={`${alignTdClass(col.align)} text-secondary`}>{r.next_unlock_date || '—'}</td>
    case 'unlock_float_ratio':
      if (isBlankCell(r.unlock_float_ratio) && gaps?.gapKeys?.includes('unlock_float_ratio')) return gapTd(col)
      return <td key={col.id} className={numCls}>{r.unlock_float_ratio != null ? fmtPercentPoint(Number(r.unlock_float_ratio)) : '—'}</td>
    case 'weekly_change_pct':
      if (isBlankCell(r.weekly_change_pct) && gaps?.gapKeys?.includes('weekly_change_pct')) return gapTd(col)
      return <td key={col.id} className={`${numCls} font-medium ${priceColorClass(r.weekly_change_pct)}`}>{fmtPct(r.weekly_change_pct)}</td>
    case 'monthly_change_pct':
      if (isBlankCell(r.monthly_change_pct) && gaps?.gapKeys?.includes('monthly_change_pct')) return gapTd(col)
      return <td key={col.id} className={`${numCls} font-medium ${priceColorClass(r.monthly_change_pct)}`}>{fmtPct(r.monthly_change_pct)}</td>
    case 'up_streak':
      if (isBlankCell(r.up_streak) && gaps?.gapKeys?.includes('up_streak')) return gapTd(col)
      return streakBadge(col, Number(r.up_streak), 'up')
    case 'down_streak':
      if (isBlankCell(r.down_streak) && gaps?.gapKeys?.includes('down_streak')) return gapTd(col)
      return streakBadge(col, Number(r.down_streak), 'down')
    case 'benefit_ratio':
      if (isBlankCell(r.benefit_ratio) && gaps?.gapKeys?.includes('benefit_ratio')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtUnsignedRatio(r.benefit_ratio)}</td>
    case 'avg_cost_bias':
      if (isBlankCell(r.avg_cost_bias) && gaps?.gapKeys?.includes('avg_cost_bias')) return gapTd(col)
      return <td key={col.id} className={`${numCls} font-medium ${priceColorClass(r.avg_cost_bias)}`}>{fmtPct(r.avg_cost_bias)}</td>
    case 'concentration_90':
      if (isBlankCell(r.concentration_90) && gaps?.gapKeys?.includes('concentration_90')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtUnsignedRatio(r.concentration_90)}</td>
    case 'concentration_70':
      if (isBlankCell(r.concentration_70) && gaps?.gapKeys?.includes('concentration_70')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtUnsignedRatio(r.concentration_70)}</td>
    case 'macd_dif_w':
      if (isBlankCell(r.macd_dif_w) && gaps?.gapKeys?.includes('macd_dif_w')) return gapTd(col)
      return <td key={col.id} className={`${numCls} ${priceColorClass(r.macd_hist_w)}`}>{r.macd_dif_w != null ? fmtPrice(r.macd_dif_w) : '—'}</td>
    case 'macd_dea_w':
      if (isBlankCell(r.macd_dea_w) && gaps?.gapKeys?.includes('macd_dea_w')) return gapTd(col)
      return <td key={col.id} className={numCls}>{r.macd_dea_w != null ? fmtPrice(r.macd_dea_w) : '—'}</td>
    case 'macd_hist_w':
      if (isBlankCell(r.macd_hist_w) && gaps?.gapKeys?.includes('macd_hist_w')) return gapTd(col)
      return <td key={col.id} className={`${numCls} ${priceColorClass(r.macd_hist_w)}`}>{r.macd_hist_w != null ? fmtPrice(r.macd_hist_w) : '—'}</td>
    case 'kdj_k_w':
      if (isBlankCell(r.kdj_k_w) && gaps?.gapKeys?.includes('kdj_k_w')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_k_w, 1)}</td>
    case 'kdj_d_w':
      if (isBlankCell(r.kdj_d_w) && gaps?.gapKeys?.includes('kdj_d_w')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_d_w, 1)}</td>
    case 'kdj_j_w':
      if (isBlankCell(r.kdj_j_w) && gaps?.gapKeys?.includes('kdj_j_w')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_j_w, 1)}</td>
    case 'macd_dif_m':
      if (isBlankCell(r.macd_dif_m) && gaps?.gapKeys?.includes('macd_dif_m')) return gapTd(col)
      return <td key={col.id} className={`${numCls} ${priceColorClass(r.macd_hist_m)}`}>{r.macd_dif_m != null ? fmtPrice(r.macd_dif_m) : '—'}</td>
    case 'macd_dea_m':
      if (isBlankCell(r.macd_dea_m) && gaps?.gapKeys?.includes('macd_dea_m')) return gapTd(col)
      return <td key={col.id} className={numCls}>{r.macd_dea_m != null ? fmtPrice(r.macd_dea_m) : '—'}</td>
    case 'macd_hist_m':
      if (isBlankCell(r.macd_hist_m) && gaps?.gapKeys?.includes('macd_hist_m')) return gapTd(col)
      return <td key={col.id} className={`${numCls} ${priceColorClass(r.macd_hist_m)}`}>{r.macd_hist_m != null ? fmtPrice(r.macd_hist_m) : '—'}</td>
    case 'kdj_k_m':
      if (isBlankCell(r.kdj_k_m) && gaps?.gapKeys?.includes('kdj_k_m')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_k_m, 1)}</td>
    case 'kdj_d_m':
      if (isBlankCell(r.kdj_d_m) && gaps?.gapKeys?.includes('kdj_d_m')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_d_m, 1)}</td>
    case 'kdj_j_m':
      if (isBlankCell(r.kdj_j_m) && gaps?.gapKeys?.includes('kdj_j_m')) return gapTd(col)
      return <td key={col.id} className={numCls}>{fmtFixed(r.kdj_j_m, 1)}</td>
    case 'pb':            return <td key={col.id} className={numCls}>{r.pb != null ? fmtPrice(r.pb) : '—'}</td>
    case 'gross_margin':  return <td key={col.id} className={numCls}>{r.gross_margin != null ? fmtPct(r.gross_margin) : '—'}</td>
    case 'net_margin':    return <td key={col.id} className={numCls}>{r.net_margin != null ? fmtPct(r.net_margin) : '—'}</td>
    case 'revenue_yoy':   return <td key={col.id} className={numCls}>{r.revenue_yoy != null ? fmtPct(r.revenue_yoy) : '—'}</td>
    case 'net_income_yoy':return <td key={col.id} className={numCls}>{r.net_income_yoy != null ? fmtPct(r.net_income_yoy) : '—'}</td>
    case 'debt_ratio':    return <td key={col.id} className={numCls}>{r.debt_ratio != null ? fmtPct(r.debt_ratio) : '—'}</td>
    default:
      return <td key={col.id} className={`${alignTdClass(col.align)} text-muted`}>—</td>
  }
}

/** 根据列对齐返回 td 基础 class（不含 num 样式） */
function alignTdClass(align: ColumnConfig['align']): string {
  if (align === 'right') return 'px-3 py-2 text-right'
  if (align === 'center') return 'px-3 py-2 text-center'
  return 'px-3 py-2'
}
