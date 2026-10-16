import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type FundFlowAmountRow, type FundFlowBoard, type FundFlowMarginDay, type FundFlowSectorBlock, type FundFlowStockBlock } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

function yi(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  const n = value / 1e8
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(2)} 亿`
}

function signedClass(value: number | null | undefined): string {
  if (value == null || value === 0) return 'text-muted'
  return value > 0 ? 'text-danger' : 'text-bear'
}

function Empty({ text }: { text: string }) {
  return <p className="text-sm text-muted">{text}</p>
}

function Card({
  title,
  meta,
  children,
}: {
  title: string
  meta?: string
  children: ReactNode
}) {
  return (
    <section className="min-w-0 rounded-md border border-border p-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium">{title}</h2>
        {meta ? <span className="text-[11px] text-muted">{meta}</span> : null}
      </div>
      {children}
    </section>
  )
}

function Tabs<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: { key: T; label: string }[]
  onChange: (key: T) => void
}) {
  return (
    <div className="mb-2 flex gap-2">
      {options.map(item => (
        <button
          key={item.key}
          type="button"
          className={cn(
            'rounded-md border px-2.5 py-1 text-xs',
            value === item.key
              ? 'border-accent bg-accent/10 text-foreground'
              : 'border-border text-muted',
          )}
          onClick={() => onChange(item.key)}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

function AmountList({
  rows,
  label,
  amount,
  emptyText = '还没有这一块的落盘。',
  renderLabel,
}: {
  rows: FundFlowAmountRow[]
  label: (row: FundFlowAmountRow) => string
  amount: (row: FundFlowAmountRow) => number | null | undefined
  emptyText?: string
  renderLabel?: (row: FundFlowAmountRow) => ReactNode
}) {
  if (rows.length === 0) return <Empty text={emptyText} />
  return (
    <ul className="space-y-1.5">
      {rows.map((row, index) => {
        const value = amount(row)
        return (
          <li key={`${label(row)}-${index}`} className="flex items-baseline justify-between gap-3 text-sm">
            <span className="flex min-w-0 items-baseline">
              {row.rank != null ? <span className="mr-2 text-xs text-muted">{row.rank}</span> : null}
              {renderLabel ? renderLabel(row) : <span className="min-w-0 truncate">{label(row)}</span>}
            </span>
            <span className={cn('shrink-0 font-mono text-xs', signedClass(value))}>{yi(value)}</span>
          </li>
        )
      })}
    </ul>
  )
}

function stockName(row: FundFlowAmountRow): string {
  const name = row.name?.trim()
  if (name) return name
  return row.code || '—'
}

type EtfWindow = {
  days: number
  label: string
  value: (row: FundFlowAmountRow) => number | null | undefined
}

// 份额变化的三个窗口。1 日变化与后端的 share_change 同源，单独取字段以便统一渲染。
const ETF_WINDOWS: EtfWindow[] = [
  { days: 1, label: '1日', value: row => row.change_1d ?? row.share_change },
  { days: 5, label: '5日', value: row => row.change_5d },
  { days: 15, label: '15日', value: row => row.change_15d },
]

function formatShares(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  const n = value / 1e8
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}亿份`
}

// 份额推算和领航者稿都已经是亿元，不再除 1 亿。
function formatYiYuan(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}亿元`
}

const ETF_INFLOW_FIELDS: { key: 'net_inflow_1d' | 'net_inflow_5d' | 'net_inflow_20d'; label: string }[] = [
  { key: 'net_inflow_1d', label: '1日' },
  { key: 'net_inflow_5d', label: '5日' },
  { key: 'net_inflow_20d', label: '20日' },
]

function navigatorInflow(row: FundFlowAmountRow): string | null {
  if (row.flow_net == null && row.flow_net_5d == null && row.flow_net_20d == null) return null
  const parts = [`领航者净流入 ${formatYiYuan(row.flow_net)}`]
  if (row.flow_net_5d != null) parts.push(`5日 ${formatYiYuan(row.flow_net_5d)}`)
  if (row.flow_net_20d != null) parts.push(`20日 ${formatYiYuan(row.flow_net_20d)}`)
  return parts.join(' · ')
}

function etfBaselineTitle(
  baselines: Record<string, string | null> | undefined,
  days: number,
): string {
  const base = baselines?.[String(days)]
  // 说「份」而不是「交易日」：etf_shares 中间可能缺天，那时「前 N 个交易日」
  // 会不准；落盘记录本身才是可靠锚点。
  return base ? `较 ${base}（前 ${days} 份落盘）` : `不足 ${days} 份落盘，无法计算`
}

function StockLabel({ row }: { row: FundFlowAmountRow }) {
  const name = row.name?.trim()
  const code = row.code || ''
  return (
    <span className="min-w-0 truncate">
      {stockName(row)}
      {name && code ? <span className="ml-2 text-[11px] text-muted">{code}</span> : null}
    </span>
  )
}

function FlowSides({
  inflow,
  outflow,
  label,
  amount,
  emptyInflow,
  renderLabel,
}: {
  inflow: FundFlowAmountRow[]
  outflow: FundFlowAmountRow[]
  label: (row: FundFlowAmountRow) => string
  amount: (row: FundFlowAmountRow) => number | null | undefined
  emptyInflow: string
  renderLabel?: (row: FundFlowAmountRow) => ReactNode
}) {
  return (
    <div className="space-y-3">
      <div>
        <p className="mb-1 text-[11px] text-muted">净流入</p>
        <AmountList rows={inflow} label={label} amount={amount} emptyText={emptyInflow} renderLabel={renderLabel} />
      </div>
      <div>
        <p className="mb-1 text-[11px] text-muted">净流出</p>
        <AmountList rows={outflow} label={label} amount={amount} emptyText="没有净流出。" renderLabel={renderLabel} />
      </div>
    </div>
  )
}

function StockSides({
  block,
  amount,
  emptyInflow,
}: {
  block: FundFlowStockBlock | undefined
  amount: (row: FundFlowAmountRow) => number | null | undefined
  emptyInflow: string
}) {
  if (block?.partial) {
    return <Empty text={block.note || '个股样本不完整，不作为全市场排名。'} />
  }
  return (
    <FlowSides
      inflow={block?.inflow ?? block?.items ?? []}
      outflow={block?.outflow ?? []}
      label={stockName}
      amount={amount}
      emptyInflow={emptyInflow}
      renderLabel={row => <StockLabel row={row} />}
    />
  )
}

function sectorName(row: FundFlowAmountRow): string {
  return row.name || '—'
}

function SectorSides({ block }: { block: FundFlowSectorBlock | undefined }) {
  return (
    <FlowSides
      inflow={block?.inflow ?? block?.items ?? []}
      outflow={block?.outflow ?? []}
      label={sectorName}
      amount={row => row.net_inflow}
      emptyInflow="还没有这一块的落盘。"
    />
  )
}

function capturedClock(captured: string | null | undefined): string | null {
  if (!captured) return null
  const matched = captured.match(/T(\d{2}:\d{2})/)
  return matched?.[1] ?? null
}

function stockCardMeta(block: FundFlowStockBlock | undefined, window: 'today' | '5d'): string | undefined {
  const day = block?.trade_date
  if (!day) return undefined
  if (window !== 'today' || !block?.snapshot) return day
  const clock = capturedClock(block.captured_at)
  if (block.snapshot === 'close') return clock ? `${day} · 收盘 · ${clock}` : `${day} · 收盘`
  // 收盘后采用的盘中快照是「收盘前」的定格，标出来免得被当成实时。
  if (block.stale_after_close) {
    return clock ? `${day} · 收盘前快照 · ${clock}` : `${day} · 收盘前快照`
  }
  return clock ? `${day} · 盘中 · ${clock}` : `${day} · 盘中`
}

function fallbackMarginDays(items: FundFlowAmountRow[]): FundFlowMarginDay[] {
  const byDate = new Map<string, FundFlowMarginDay>()
  for (const row of items) {
    const day = row.trade_date || ''
    if (!day || row.margin_balance == null) continue
    const slot = byDate.get(day) ?? {
      trade_date: day,
      margin_balance: 0,
      coverage: 'none',
      label: '',
    }
    slot.margin_balance = (slot.margin_balance ?? 0) + row.margin_balance
    if (row.market === 'sse') slot.sse_margin_balance = row.margin_balance
    if (row.market === 'szse') slot.szse_margin_balance = row.margin_balance
    byDate.set(day, slot)
  }
  return [...byDate.values()].sort((a, b) => a.trade_date < b.trade_date ? -1 : 1).map(day => {
    const markets = [
      day.sse_margin_balance != null ? 'sse' : '',
      day.szse_margin_balance != null ? 'szse' : '',
    ].filter(Boolean)
    if (markets.length === 2) return { ...day, coverage: 'both', label: '沪深合计' }
    if (markets.length === 1) {
      const only = markets[0]
      return { ...day, coverage: only, label: only === 'sse' ? '仅沪市' : '仅深市' }
    }
    return { ...day, label: '汇总' }
  })
}

function MarginSplit({ day }: { day: FundFlowMarginDay }) {
  if (day.coverage !== 'both' || day.sse_margin_balance == null || day.szse_margin_balance == null) {
    return null
  }
  return (
    <span className="mt-0.5 block text-[11px] text-muted">
      沪 {yi(day.sse_margin_balance).replace('+', '')} · 深 {yi(day.szse_margin_balance).replace('+', '')}
    </span>
  )
}

export function FundFlow() {
  const [sector, setSector] = useState<'industry' | 'concept'>('industry')
  const [stockWindow, setStockWindow] = useState<'today' | '5d'>('today')
  const board = useQuery({
    queryKey: QK.fundFlowBoard,
    queryFn: () => api.fundFlowBoard(),
    staleTime: 60_000,
  })
  const data: FundFlowBoard | undefined = board.data
  const sectorBlock = sector === 'industry' ? data?.industry : data?.concept
  const stockBlock = stockWindow === 'today' ? data?.stocks_today : data?.stocks_5d
  const margin = (data?.margin.days && data.margin.days.length > 0)
    ? [...data.margin.days].sort((a, b) => a.trade_date < b.trade_date ? -1 : 1)
    : fallbackMarginDays(data?.margin.items ?? [])
  const south = [...(data?.southbound.items ?? [])].reverse()
  const etf = data?.etf_shares

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="资金流向"
        helpId="fund-flow"
        subtitle="只读本地落盘。没有数据的块留空，不把缺失写成 0。"
      />
      <div className="min-h-0 flex-1 overflow-auto p-3 sm:p-4">
        {board.isLoading && <p className="text-sm text-muted">加载中…</p>}
        {board.isError && <p className="text-sm text-danger">资金流向加载失败</p>}
        {board.isSuccess && (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <Card
              title="板块 / 概念资金排行"
              meta={sectorBlock?.trade_date ? `${sectorBlock.trade_date} · ${sectorBlock.snapshot === 'close' ? '收盘' : '盘中'}` : undefined}
            >
              <Tabs
                value={sector}
                options={[{ key: 'industry', label: '行业' }, { key: 'concept', label: '概念' }]}
                onChange={setSector}
              />
              <SectorSides block={sectorBlock} />
            </Card>

            <Card title="个股主力净流入" meta={stockCardMeta(stockBlock, stockWindow)}>
              <Tabs
                value={stockWindow}
                options={[{ key: 'today', label: '今日' }, { key: '5d', label: '5日' }]}
                onChange={setStockWindow}
              />
              <StockSides
                block={stockBlock}
                amount={row => stockWindow === 'today' ? row.main_net : row.ff_main_net_5d}
                emptyInflow={stockWindow === '5d'
                  ? '凑满 5 个交易日才列出。缺的日子不按 0 计入。'
                  : '还没有这一块的落盘。'}
              />
            </Card>

            <Card title="两融余额趋势">
              {margin.length === 0 ? <Empty text="还没有融资融券汇总。" /> : (
                <ul className="space-y-1.5">
                  {[...margin].reverse().slice(0, 12).map(day => (
                    <li key={day.trade_date} className="flex items-baseline justify-between gap-3 text-sm">
                      <span className="text-xs text-muted">{day.trade_date}</span>
                      <span className="text-right">
                        <span className="font-mono text-xs">{yi(day.margin_balance).replace('+', '')}</span>
                        <span className="ml-2 text-[11px] text-muted">{day.label || '汇总'}</span>
                        <MarginSplit day={day} />
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card title="南向资金">
              {south.length === 0 ? <Empty text="还没有南向净流入。" /> : (
                <ul className="space-y-1.5">
                  {south.slice(0, 12).map(row => (
                    <li key={row.trade_date} className="flex items-baseline justify-between gap-3 text-sm">
                      <span className="text-xs text-muted">{row.trade_date}</span>
                      <span className={cn('font-mono text-xs', signedClass(row.net_flow))}>{yi(row.net_flow)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card
              title="ETF 份额变化"
              meta={etf?.trade_date ? `${etf.prev_trade_date ? `${etf.prev_trade_date} → ` : ''}${etf.trade_date}` : undefined}
            >
              {(etf?.items.length ?? 0) === 0 ? <Empty text="还没有 ETF 份额。只有一天时只显示份额，不编造变化。" /> : (
                <div className="min-w-0">
                  <p className="mb-2 text-[11px] leading-relaxed text-muted">
                    右侧是份额变化。净流入 = (当日份额 − 上一份落盘) × 收盘价，单位亿元；5 日 / 20 日是相邻交易日的滚动合计。复权跳变或中间缺交易日时留空。
                  </p>
                  <div className="mb-1 flex items-baseline justify-between gap-3 text-[11px] text-muted">
                    <span>名称</span>
                    <span className="flex shrink-0 gap-2 sm:gap-3">
                      {ETF_WINDOWS.map(window => (
                        <span key={window.label} className="w-[56px] text-right sm:w-[68px]">
                          {window.label}
                        </span>
                      ))}
                    </span>
                  </div>
                  <ul className="space-y-2">
                    {etf?.items.map(row => {
                      const navigator = navigatorInflow(row)
                      return (
                        <li
                          key={row.code}
                          className={cn(
                            'min-w-0',
                            row.flow_diverged && 'rounded border border-warning/40 bg-warning/10 px-1.5 py-1',
                          )}
                          title={row.flow_diverged ? '推算净流入与领航者相差超过 20%' : undefined}
                        >
                          <div className="flex items-baseline justify-between gap-3 text-sm">
                            <span className="min-w-0 truncate">
                              {row.name || row.code}
                              {row.broad ? (
                                <span className="ml-2 rounded border border-accent/40 px-1 py-0.5 text-[10px] text-accent">
                                  {row.broad}
                                  {row.broad === '沪深300' || row.broad === '科创50' ? ' · 国家队代理' : ''}
                                </span>
                              ) : null}
                            </span>
                            <span className="flex shrink-0 gap-2 sm:gap-3">
                              {ETF_WINDOWS.map(window => (
                                <span
                                  key={window.label}
                                  className={cn('w-[56px] text-right font-mono text-xs sm:w-[68px]', signedClass(window.value(row)))}
                                  title={etfBaselineTitle(etf?.baselines, window.days)}
                                >
                                  {formatShares(window.value(row))}
                                </span>
                              ))}
                            </span>
                          </div>
                          <div className="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-[11px]">
                            <span className="text-muted">净流入</span>
                            {ETF_INFLOW_FIELDS.map(field => (
                              <span key={field.key} className={cn('font-mono', signedClass(row[field.key]))}>
                                {field.label} {formatYiYuan(row[field.key])}
                              </span>
                            ))}
                          </div>
                          <div className={cn('mt-0.5 text-[11px]', row.flow_diverged ? 'text-warning' : 'text-muted')}>
                            {row.code}
                            {row.shares != null ? ` · 份额 ${(row.shares / 1e8).toFixed(2)} 亿份` : ''}
                            {navigator ? ` · ${navigator}` : ''}
                            {row.flow_diverged ? ' · 与领航者相差超过 20%' : ''}
                          </div>
                        </li>
                      )
                    })}
                  </ul>
                </div>
              )}
            </Card>
          </div>
        )}
      </div>
    </div>
  )
}
