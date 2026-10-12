import type { ChartMarker, ChartPriceLine } from '@/components/EChartsCandlestick'
import { fmtPct } from '@/lib/format'

/** 买卖点跟随站点涨跌色：买用红涨 --bull，卖用绿跌 --bear。 */
export const TRADE_BUY = '#F04438'
export const TRADE_SELL = '#12B76A'

const DSA_COLOR: Record<TradeLevelKind, string> = {
  buy: TRADE_BUY,
  target: '#A855F7',
  support: '#3B82F6',
  resistance: '#F97316',
  stop: TRADE_SELL,
}

const DSA_SHORT: Record<TradeLevelKind, string> = {
  buy: '买入',
  stop: '止损',
  target: '目标',
  support: '支撑',
  resistance: '压力',
}

/** 日线买卖点在分时图上的落点：收盘分钟。 */
export const DAILY_MARK_MINUTE = '15:00'

export interface TradeMarkStrategy {
  id: string
  name: string
  source: string
  tags: string[]
  fundamental: boolean
}

export interface TradeSignalMarker {
  id: string
  date: string
  side: 'buy' | 'sell'
  style: 'triangle' | 'breakout'
  rule: string
  price: number | null
  fwd5: number | null
  fwd20: number | null
  /** 日线信号对齐到收盘分钟。缺省时分时图用 15:00。 */
  time?: string | null
}

export type TradeLevelKind = 'buy' | 'stop' | 'target' | 'support' | 'resistance'

export interface TradeLevel {
  kind: TradeLevelKind
  label: string
  price: number
}

export interface TradeFill {
  date: string
  side: 'buy' | 'sell'
  price: number
  qty: number
  account: string
  /** 成交时刻 HH:MM。没有时钟时分时图回退到收盘分钟。 */
  time?: string | null
}

export interface TradeTMark {
  time: string
  rule: string
  side: 'buy' | 'sell' | 'neutral'
  price: number | null
  date?: string | null
}

export interface TradeTBlock {
  date: string | null
  band: number
  cooldown_min: number
  marks: TradeTMark[]
}

export interface TradeSignalStats {
  signal_count: number
  win_rate: number | null
  avg_fwd20: number | null
  win_sample: number
  note?: string
}

export interface TradeSignalsResponse {
  symbol: string
  strategy: string | null
  strategies: TradeMarkStrategy[]
  markers: TradeSignalMarker[]
  stats: TradeSignalStats
  levels: TradeLevel[]
  fills: TradeFill[]
  t_trade: TradeTBlock
}

export interface TradeMarkDetail {
  id: string
  rule: string
  date: string
  price: number | null
  fwd5: number | null
  fwd20: number | null
}

export function formatForward(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return fmtPct(value)
}

export function strategyMarkers(markers: TradeSignalMarker[], enabled: boolean): ChartMarker[] {
  if (!enabled) return []
  return markers.map(marker => ({
    date: marker.date.slice(0, 10),
    kind: marker.side,
    style: marker.style === 'breakout' ? 'breakout' : 'triangle',
    markerId: marker.id,
    color: marker.side === 'buy' ? TRADE_BUY : TRADE_SELL,
  }))
}

export function fillMarkers(fills: TradeFill[], enabled: boolean): ChartMarker[] {
  if (!enabled) return []
  return fills.map(fill => ({
    date: fill.date.slice(0, 10),
    kind: fill.side,
    style: 'circle' as const,
    markerId: fillMarkerId(fill),
    color: fill.side === 'buy' ? TRADE_BUY : TRADE_SELL,
  }))
}

export function fillMarkerId(fill: TradeFill): string {
  return `fill:${fill.account}:${fill.date}:${fill.side}:${fill.price}`
}

export function dsaLines(levels: TradeLevel[], enabled: boolean, short = false): ChartPriceLine[] {
  if (!enabled) return []
  return levels
    .filter(level => Number.isFinite(level.price) && level.price > 0)
    .map(level => ({
      value: level.price,
      label: `${short ? (DSA_SHORT[level.kind] ?? level.label) : level.label} ${level.price.toFixed(2)}`,
      color: DSA_COLOR[level.kind] ?? '#A1A1AA',
    }))
}

export function markerDetails(markers: TradeSignalMarker[], fills: TradeFill[]): Map<string, TradeMarkDetail> {
  const details = new Map<string, TradeMarkDetail>()
  for (const marker of markers) {
    details.set(marker.id, {
      id: marker.id,
      rule: marker.rule,
      date: marker.date.slice(0, 10),
      price: marker.price,
      fwd5: marker.fwd5,
      fwd20: marker.fwd20,
    })
  }
  for (const fill of fills) {
    details.set(fillMarkerId(fill), {
      id: fillMarkerId(fill),
      rule: `模拟盘成交 · ${fill.account} · ${fill.qty}股`,
      date: fill.date.slice(0, 10),
      price: fill.price,
      fwd5: null,
      fwd20: null,
    })
  }
  return details
}

export type IntradayMarkSource = 'strategy' | 'paper' | 't'
export type IntradayMarkStyle = 'triangle' | 'breakout' | 'circle' | 'dot'

export interface IntradayTradeMark {
  date: string
  time: string
  side: 'buy' | 'sell' | 'neutral'
  style: IntradayMarkStyle
  color: string
  label?: string
  tip: string
  price: number | null
  source: IntradayMarkSource
}

function finitePrice(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

function priceText(value: number | null): string {
  return value == null ? '—' : value.toFixed(2)
}

function sideWord(side: 'buy' | 'sell' | 'neutral'): string {
  if (side === 'buy') return '买入'
  if (side === 'sell') return '卖出'
  return '中性'
}

/** 把时钟收进连续竞价分钟。轴上没有的时刻不丢点，也不抛错。 */
export function sessionMinute(value: string | null | undefined, fallback: string | null = null): string | null {
  const match = /(\d{2}):(\d{2})/.exec(String(value ?? ''))
  const raw = match ? `${match[1]}:${match[2]}` : fallback
  if (!raw) return null
  if (raw < '09:30') return '09:30'
  if (raw > '11:30' && raw < '13:00') return '11:30'
  if (raw > '15:00') return '15:00'
  return raw
}

export function intradayTradeMarks(input: {
  markers?: TradeSignalMarker[] | null
  fills?: TradeFill[] | null
  tMarks?: TradeTMark[] | null
  showStrategy?: boolean
  showPaper?: boolean
  showT?: boolean
  /** 只保留这一天。多日分时不传。 */
  date?: string | null
  /** 做T标记自己没带日期时用。 */
  tDate?: string | null
}): IntradayTradeMark[] {
  const out: IntradayTradeMark[] = []
  const only = input.date ? input.date.slice(0, 10) : ''
  const keep = (day: string) => !only || day === only
  if (input.showStrategy) {
    for (const marker of input.markers ?? []) {
      const day = String(marker?.date ?? '').slice(0, 10)
      const time = sessionMinute(marker?.time, DAILY_MARK_MINUTE)
      const price = finitePrice(marker?.price)
      if (!day || !time || !keep(day) || (marker.side !== 'buy' && marker.side !== 'sell')) continue
      out.push({
        date: day,
        time,
        side: marker.side,
        style: marker.style === 'breakout' ? 'breakout' : 'triangle',
        color: marker.side === 'buy' ? TRADE_BUY : TRADE_SELL,
        tip: `策略信号 · ${sideWord(marker.side)} ${priceText(price)}`,
        price,
        source: 'strategy',
      })
    }
  }
  if (input.showPaper) {
    for (const fill of input.fills ?? []) {
      const day = String(fill?.date ?? '').slice(0, 10)
      const time = sessionMinute(fill?.time, DAILY_MARK_MINUTE)
      const price = finitePrice(fill?.price)
      if (!day || !time || !keep(day) || (fill.side !== 'buy' && fill.side !== 'sell')) continue
      out.push({
        date: day,
        time,
        side: fill.side,
        style: 'circle',
        color: fill.side === 'buy' ? TRADE_BUY : TRADE_SELL,
        label: fill.side === 'buy' ? 'B' : 'S',
        tip: `模拟成交 · ${sideWord(fill.side)} ${priceText(price)}`,
        price,
        source: 'paper',
      })
    }
  }
  if (input.showT) {
    for (const mark of input.tMarks ?? []) {
      const day = String(mark?.date || input.tDate || only).slice(0, 10)
      const time = sessionMinute(mark?.time, null)
      const price = finitePrice(mark?.price)
      const side = mark?.side === 'buy' || mark?.side === 'sell' ? mark.side : 'neutral'
      if (!day || !time || !keep(day)) continue
      out.push({
        date: day,
        time,
        side,
        style: 'dot',
        color: side === 'buy' ? TRADE_BUY : side === 'sell' ? TRADE_SELL : '#A1A1AA',
        label: 'T',
        tip: `做T提醒 · ${mark.rule || sideWord(side)} ${priceText(price)}`,
        price,
        source: 't',
      })
    }
  }
  return out
}

/** 某一天的分时响应已经带了做T时，用这批 K 的标记替换买卖点接口里可能滞后的做T。 */
export function preferBarTMarks(
  signalMarks: IntradayTradeMark[],
  barMarks: IntradayTradeMark[],
  coveredDates: Iterable<string>,
): IntradayTradeMark[] {
  const covered = new Set(Array.from(coveredDates, day => String(day).slice(0, 10)).filter(Boolean))
  if (covered.size === 0) return signalMarks
  return [
    ...signalMarks.filter(mark => mark.source !== 't' || !covered.has(mark.date)),
    ...barMarks,
  ]
}

export function intradayMarkPoints(
  marks: IntradayTradeMark[] | null | undefined,
  place: (mark: IntradayTradeMark) => { x: string; y: number } | null,
): any[] {
  const data: any[] = []
  for (const mark of marks ?? []) {
    const point = place(mark)
    if (!point?.x || !Number.isFinite(point.y) || point.y <= 0) continue
    const triangle = mark.style === 'triangle'
    data.push({
      name: mark.tip,
      tip: mark.tip,
      coord: [point.x, point.y],
      symbol: mark.style === 'breakout' ? 'diamond' : triangle ? 'triangle' : 'circle',
      symbolSize: mark.style === 'dot' ? 10 : mark.style === 'circle' ? 14 : 12,
      symbolRotate: triangle && mark.side === 'sell' ? 180 : 0,
      itemStyle: { color: mark.color },
      label: mark.label
        ? { show: true, formatter: mark.label, color: '#fff', fontSize: 8, fontWeight: 'bold' }
        : { show: false },
    })
  }
  return data
}
