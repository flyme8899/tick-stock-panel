import type { ChartMarker, ChartPriceLine } from '@/components/EChartsCandlestick'
import { fmtPct } from '@/lib/format'

/** 买卖点颜色按语义固定：买绿、卖红，不跟随 A 股涨跌色。 */
export const TRADE_BUY = '#12B76A'
export const TRADE_SELL = '#F04438'

const DSA_COLOR: Record<TradeLevelKind, string> = {
  support: '#3B82F6',
  resistance: '#F97316',
  stop: TRADE_SELL,
}

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
}

export type TradeLevelKind = 'support' | 'resistance' | 'stop'

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
}

export interface TradeTMark {
  time: string
  rule: string
  side: 'buy' | 'sell' | 'neutral'
  price: number | null
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
  t_trade: {
    date: string | null
    band: number
    cooldown_min: number
    marks: TradeTMark[]
  }
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

export function dsaLines(levels: TradeLevel[], enabled: boolean): ChartPriceLine[] {
  if (!enabled) return []
  return levels
    .filter(level => Number.isFinite(level.price) && level.price > 0)
    .map(level => ({
      value: level.price,
      label: `${level.label} ${level.price.toFixed(2)}`,
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
