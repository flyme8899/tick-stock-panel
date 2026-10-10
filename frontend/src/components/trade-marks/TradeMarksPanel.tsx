import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { ChartMarker, ChartPriceLine } from '@/components/EChartsCandlestick'
import { ApiError, api } from '@/lib/api'
import { fmtPct } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import {
  dsaLines,
  fillMarkers,
  formatForward,
  markerDetails,
  strategyMarkers,
  type TradeMarkDetail,
  type TradeTMark,
} from '@/lib/trade-marks'

export interface TradeMarkOverlayPrefs {
  strategy: boolean
  t: boolean
  dsa: boolean
  paper: boolean
  strategyId: string
}

export const DEFAULT_TRADE_MARK_OVERLAY: TradeMarkOverlayPrefs = {
  strategy: true,
  t: true,
  dsa: true,
  paper: true,
  strategyId: '',
}

export interface TradeMarksChartBindings {
  markers: ChartMarker[]
  priceLines: ChartPriceLine[]
  onMarkerHover: (markerId: string | null, point?: { x: number; y: number }) => void
  vwapBand: number | null
  tMarks: TradeTMark[]
  showT: boolean
}

interface Props {
  symbol: string
  start: string
  end: string
  intradayDate?: string | null
  extraMarkers?: ChartMarker[]
  extraPriceLines?: ChartPriceLine[]
  children: (overlay: TradeMarksChartBindings) => ReactNode
}

const TOGGLES: { key: keyof Pick<TradeMarkOverlayPrefs, 'strategy' | 't' | 'dsa' | 'paper'>; label: string }[] = [
  { key: 'strategy', label: '策略信号' },
  { key: 't', label: '做T提醒' },
  { key: 'dsa', label: 'DSA价位' },
  { key: 'paper', label: '模拟盘成交' },
]

function fmtPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(2)
}

function SignalCard({ detail }: { detail: TradeMarkDetail }) {
  return (
    <div className="w-56 rounded border border-border bg-surface/95 px-2.5 py-2 text-[11px] shadow-lg">
      <div className="font-medium leading-snug text-foreground">{detail.rule}</div>
      <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-secondary">
        <span>日期 {detail.date}</span>
        <span>价格 {fmtPrice(detail.price)}</span>
        <span>后5日 {formatForward(detail.fwd5)}</span>
        <span>后20日 {formatForward(detail.fwd20)}</span>
      </div>
    </div>
  )
}

export function TradeMarksOverlay({
  symbol,
  start,
  end,
  intradayDate,
  extraMarkers,
  extraPriceLines,
  children,
}: Props) {
  const [prefs, setPrefs] = useState<TradeMarkOverlayPrefs>(() => (
    storage.tradeMarkOverlay.get(DEFAULT_TRADE_MARK_OVERLAY)
  ))
  const [activeId, setActiveId] = useState<string | null>(null)
  const [point, setPoint] = useState<{ x: number; y: number } | null>(null)
  const pinnedRef = useRef(false)

  const update = useCallback((patch: Partial<TradeMarkOverlayPrefs>) => {
    setPrefs(prev => {
      const next = { ...prev, ...patch }
      storage.tradeMarkOverlay.set(next)
      return next
    })
  }, [])

  const anyOn = prefs.strategy || prefs.t || prefs.dsa || prefs.paper
  const strategyParam = prefs.strategy && prefs.strategyId ? prefs.strategyId : ''
  const intraday = intradayDate ?? ''
  const query = useQuery({
    queryKey: QK.tradeSignals(symbol, strategyParam, start, end, intraday),
    queryFn: () => api.tradeSignals(symbol, {
      strategy: strategyParam || undefined,
      start,
      end,
      intraday: intraday || undefined,
    }),
    enabled: !!symbol && !!start && !!end && anyOn,
    staleTime: 60_000,
  })

  useEffect(() => {
    if (query.error instanceof ApiError && query.error.status === 404 && prefs.strategyId) {
      update({ strategyId: '' })
    }
  }, [prefs.strategyId, query.error, update])

  const data = query.data
  const strategies = data?.strategies ?? []
  const showStrategyMarks = prefs.strategy && !!prefs.strategyId
  const markers = useMemo(() => [
    ...(extraMarkers ?? []),
    ...strategyMarkers(data?.markers ?? [], showStrategyMarks),
    ...fillMarkers(data?.fills ?? [], prefs.paper),
  ], [data?.fills, data?.markers, extraMarkers, prefs.paper, showStrategyMarks])
  const priceLines = useMemo(() => [
    ...(extraPriceLines ?? []),
    ...dsaLines(data?.levels ?? [], prefs.dsa),
  ], [data?.levels, extraPriceLines, prefs.dsa])
  const tMarks = useMemo(
    () => (prefs.t ? data?.t_trade?.marks ?? [] : []),
    [data?.t_trade?.marks, prefs.t],
  )
  const details = useMemo(
    () => markerDetails(data?.markers ?? [], data?.fills ?? []),
    [data?.fills, data?.markers],
  )
  const active = activeId ? details.get(activeId) ?? null : null

  const onMarkerHover = useCallback((id: string | null, next?: { x: number; y: number }) => {
    if (id == null) {
      if (pinnedRef.current) return
      setActiveId(null)
      setPoint(null)
      return
    }
    pinnedRef.current = false
    setActiveId(id)
    setPoint(next ?? null)
  }, [])

  const selectRow = useCallback((id: string) => {
    pinnedRef.current = true
    setActiveId(id)
    setPoint(null)
  }, [])

  const stats = data?.stats
  const card = active ? <SignalCard detail={active} /> : null

  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 overflow-x-auto pb-0.5">
        <span className="shrink-0 text-[10px] text-muted">买卖点</span>
        {TOGGLES.map(item => (
          <button
            key={item.key}
            type="button"
            onClick={() => update({ [item.key]: !prefs[item.key] })}
            className={`shrink-0 rounded px-2 py-0.5 text-[10px] font-mono ${
              prefs[item.key] ? 'bg-accent/20 text-accent' : 'bg-elevated text-muted hover:text-secondary'
            }`}
          >
            {item.label}
          </button>
        ))}
        {prefs.strategy && (
          <select
            aria-label="策略信号"
            value={prefs.strategyId}
            onChange={event => update({ strategyId: event.target.value })}
            className="h-6 max-w-[200px] shrink-0 rounded border border-border bg-base px-1 text-[10px] text-secondary outline-none"
          >
            <option value="">选择策略</option>
            {prefs.strategyId && !strategies.some(row => row.id === prefs.strategyId) && (
              <option value={prefs.strategyId}>{prefs.strategyId}</option>
            )}
            {strategies.map(row => (
              <option key={row.id} value={row.id}>{row.name}</option>
            ))}
          </select>
        )}
        {query.isFetching && <span className="shrink-0 text-[10px] text-muted">计算中</span>}
        {query.isError && <span className="shrink-0 text-[10px] text-danger">买卖点暂不可用</span>}
      </div>

      <div className="flex flex-col gap-3 xl:flex-row xl:items-start">
        <div className="relative min-w-0 flex-1">
          {children({
            markers,
            priceLines,
            onMarkerHover,
            vwapBand: prefs.t ? (data?.t_trade?.band ?? 0.015) : null,
            tMarks,
            showT: prefs.t,
          })}
          {card && (
            <div
              className="pointer-events-none absolute z-30 hidden md:block"
              style={{
                left: point ? Math.max(8, Math.min(point.x + 12, 280)) : 8,
                top: point ? Math.max(8, point.y + 12) : 8,
              }}
            >
              {card}
            </div>
          )}
          {card && <div className="mt-2 md:hidden">{card}</div>}
        </div>

        {prefs.strategy && (
          <aside className="w-full shrink-0 rounded border border-border/70 bg-surface/50 p-2.5 xl:w-[280px]">
            <div className="text-xs font-medium text-foreground">该策略在本股历史表现</div>
            {!prefs.strategyId ? (
              <p className="mt-2 text-[11px] text-muted">选择策略后计算本股历史买卖点</p>
            ) : (
              <>
                <div className="mt-2 grid grid-cols-3 gap-2 text-center">
                  <Stat label="信号数" value={String(stats?.signal_count ?? 0)} />
                  <Stat label="胜率" value={stats ? fmtPct(stats.win_rate) : '—'} title="后20日收益大于 0 的买入占比" />
                  <Stat label="平均后20日" value={stats ? fmtPct(stats.avg_fwd20) : '—'} />
                </div>
                {stats?.note && <p className="mt-2 text-[11px] leading-snug text-muted">{stats.note}</p>}
                <div className="mt-2 max-h-64 overflow-auto">
                  <table className="w-full text-left text-[10px]">
                    <thead className="text-muted">
                      <tr>
                        <th className="py-1 pr-1 font-normal">日期</th>
                        <th className="py-1 pr-1 font-normal">方向</th>
                        <th className="py-1 pr-1 font-normal">价格</th>
                        <th className="py-1 pr-1 font-normal">后5日</th>
                        <th className="py-1 pr-1 font-normal">后20日</th>
                        <th className="py-1 font-normal">规则</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(data?.markers ?? []).length === 0 && (
                        <tr>
                          <td colSpan={6} className="py-2 text-muted">这段区间没有买卖点</td>
                        </tr>
                      )}
                      {(data?.markers ?? []).map(row => (
                        <tr
                          key={row.id}
                          onClick={() => selectRow(row.id)}
                          className={`cursor-pointer border-t border-border/40 ${activeId === row.id ? 'bg-accent/10' : 'hover:bg-elevated'}`}
                        >
                          <td className="py-1 pr-1 font-mono">{row.date.slice(5)}</td>
                          <td className="py-1 pr-1" style={{ color: row.side === 'buy' ? '#12B76A' : '#F04438' }}>
                            {row.side === 'buy' ? '买' : '卖'}
                          </td>
                          <td className="py-1 pr-1 font-mono">{fmtPrice(row.price)}</td>
                          <td className="py-1 pr-1 font-mono">{formatForward(row.fwd5)}</td>
                          <td className="py-1 pr-1 font-mono">{formatForward(row.fwd20)}</td>
                          <td className="max-w-[88px] truncate py-1" title={row.rule}>{row.rule}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </aside>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div title={title}>
      <div className="text-[10px] text-muted">{label}</div>
      <div className="font-mono text-xs text-foreground">{value}</div>
    </div>
  )
}
