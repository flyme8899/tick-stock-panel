import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type FundFlowAmountRow, type FundFlowBoard } from '@/lib/api'
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
}: {
  rows: FundFlowAmountRow[]
  label: (row: FundFlowAmountRow) => string
  amount: (row: FundFlowAmountRow) => number | null | undefined
  emptyText?: string
}) {
  if (rows.length === 0) return <Empty text={emptyText} />
  return (
    <ul className="space-y-1.5">
      {rows.map((row, index) => {
        const value = amount(row)
        return (
          <li key={`${label(row)}-${index}`} className="flex items-baseline justify-between gap-3 text-sm">
            <span className="min-w-0 truncate">
              {row.rank != null ? <span className="mr-2 text-xs text-muted">{row.rank}</span> : null}
              {label(row)}
            </span>
            <span className={cn('shrink-0 font-mono text-xs', signedClass(value))}>{yi(value)}</span>
          </li>
        )
      })}
    </ul>
  )
}

function marginDays(items: FundFlowAmountRow[]) {
  const byDate = new Map<string, { total: number; markets: string[] }>()
  for (const row of items) {
    const day = row.trade_date || ''
    if (!day || row.margin_balance == null) continue
    const slot = byDate.get(day) ?? { total: 0, markets: [] }
    slot.total += row.margin_balance
    if (row.market) slot.markets.push(row.market)
    byDate.set(day, slot)
  }
  return [...byDate.entries()].sort((a, b) => a[0] < b[0] ? -1 : 1)
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
  const margin = marginDays(data?.margin.items ?? [])
  const south = [...(data?.southbound.items ?? [])].reverse()
  const etf = data?.etf_shares

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="资金流向"
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
              <AmountList
                rows={sectorBlock?.items ?? []}
                label={row => row.name || '—'}
                amount={row => row.net_inflow}
              />
            </Card>

            <Card title="个股主力净流入" meta={stockBlock?.trade_date ?? undefined}>
              <Tabs
                value={stockWindow}
                options={[{ key: 'today', label: '今日' }, { key: '5d', label: '5日' }]}
                onChange={setStockWindow}
              />
              <AmountList
                rows={stockBlock?.items ?? []}
                label={row => row.symbol || row.code || '—'}
                amount={row => stockWindow === 'today' ? row.main_net : row.ff_main_net_5d}
                emptyText={stockWindow === '5d'
                  ? '凑满 5 个交易日才列出。缺的日子不按 0 计入。'
                  : '还没有这一块的落盘。'}
              />
            </Card>

            <Card title="两融余额趋势">
              {margin.length === 0 ? <Empty text="还没有融资融券汇总。" /> : (
                <ul className="space-y-1.5">
                  {[...margin].reverse().slice(0, 12).map(([day, slot]) => (
                    <li key={day} className="flex items-baseline justify-between gap-3 text-sm">
                      <span className="text-xs text-muted">{day}</span>
                      <span className="text-right">
                        <span className="font-mono text-xs">{yi(slot.total).replace('+', '')}</span>
                        <span className="ml-2 text-[11px] text-muted">{slot.markets.join(' / ') || '汇总'}</span>
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
                <ul className="space-y-2">
                  {etf?.items.map(row => (
                    <li key={row.code} className="min-w-0">
                      <div className="flex items-baseline justify-between gap-3 text-sm">
                        <span className="min-w-0 truncate">
                          {row.name || row.code}
                          {row.broad ? (
                            <span className="ml-2 rounded border border-accent/40 px-1 py-0.5 text-[10px] text-accent">
                              {row.broad} · 国家队代理
                            </span>
                          ) : null}
                        </span>
                        <span className={cn('shrink-0 font-mono text-xs', signedClass(row.share_change))}>
                          {row.share_change == null ? '—' : yi(row.share_change).replace('亿', '亿份')}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[11px] text-muted">
                        {row.code}
                        {row.shares != null ? ` · 份额 ${(row.shares / 1e8).toFixed(2)} 亿份` : ''}
                        {row.flow_net != null ? ` · 领航者净流入 ${yi(row.flow_net)}` : ''}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>
        )}
      </div>
    </div>
  )
}
