import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
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

type EtfColumn = {
  days: number
  label: string
  shares: (row: FundFlowAmountRow) => number | null | undefined
  inflow: (row: FundFlowAmountRow) => number | null | undefined
}

// 份额变化与净流入共用 1 / 5 / 20。份额按前 N 份落盘，折算在后端完成；净流入是相邻交易日滚动合计。
const ETF_COLUMNS: EtfColumn[] = [
  {
    days: 1,
    label: '1日',
    shares: row => row.change_1d ?? row.share_change,
    inflow: row => row.net_inflow_1d,
  },
  {
    days: 5,
    label: '5日',
    shares: row => row.change_5d,
    inflow: row => row.net_inflow_5d,
  },
  {
    days: 20,
    label: '20日',
    shares: row => row.change_20d,
    inflow: row => row.net_inflow_20d,
  },
]

const ETF_COL_WIDTH = 'w-[5.75rem]'

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

function navigatorInflow(row: FundFlowAmountRow): string | null {
  if (row.flow_net == null && row.flow_net_5d == null && row.flow_net_20d == null) return null
  const parts = [`领航者净流入 ${formatYiYuan(row.flow_net)}`]
  if (row.flow_net_5d != null) parts.push(`5日 ${formatYiYuan(row.flow_net_5d)}`)
  if (row.flow_net_20d != null) parts.push(`20日 ${formatYiYuan(row.flow_net_20d)}`)
  return parts.join(' · ')
}

const ETF_BROAD_ORDER = ['沪深300', '科创50', '创业板']

/** 记下用户收起的分组。不在列表里的组保持默认展开，包括新出现的宽基。 */
export const ETF_GROUP_COLLAPSE_KEY = 'tf-fund-flow-etf-collapsed'

function readCollapsedEtfGroups(): Set<string> {
  try {
    const raw = localStorage.getItem(ETF_GROUP_COLLAPSE_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((item): item is string => typeof item === 'string' && item.length > 0))
  } catch {
    return new Set()
  }
}

function writeCollapsedEtfGroups(collapsed: ReadonlySet<string>) {
  try {
    if (collapsed.size === 0) {
      localStorage.removeItem(ETF_GROUP_COLLAPSE_KEY)
      return
    }
    localStorage.setItem(ETF_GROUP_COLLAPSE_KEY, JSON.stringify([...collapsed]))
  } catch {
    // 私隐模式写不进去时，这次会话里的折叠仍然有效。
  }
}

// 缺一只就不合计：净流入留空表示无法计算，加总会把缺口当成 0。
// 三个窗口各算各的 —— 缺 5d 不该把 1d / 20d 一起废掉。
function sumWindow(
  items: FundFlowAmountRow[],
  pick: (row: FundFlowAmountRow) => number | null | undefined,
): number | null {
  if (items.length === 0) return null
  let sum = 0
  for (const row of items) {
    const value = pick(row)
    if (value == null || Number.isNaN(value)) return null
    sum += value
  }
  return sum
}

type EtfGroupInflows = { d1: number | null; d5: number | null; d20: number | null }

function groupNetInflows(items: FundFlowAmountRow[]): EtfGroupInflows {
  return {
    d1: sumWindow(items, row => row.net_inflow_1d),
    d5: sumWindow(items, row => row.net_inflow_5d),
    d20: sumWindow(items, row => row.net_inflow_20d),
  }
}

function groupEtfRows(items: FundFlowAmountRow[]) {
  const grouped = new Map<string, FundFlowAmountRow[]>()
  const others: FundFlowAmountRow[] = []
  for (const row of items) {
    const label = row.broad?.trim()
    if (!label) {
      others.push(row)
      continue
    }
    const bucket = grouped.get(label)
    if (bucket) bucket.push(row)
    else grouped.set(label, [row])
  }
  const labels = [
    ...ETF_BROAD_ORDER.filter(label => grouped.has(label)),
    ...[...grouped.keys()].filter(label => !ETF_BROAD_ORDER.includes(label)),
  ]
  return {
    groups: labels.map(label => ({ label, items: grouped.get(label) ?? [] })),
    others,
  }
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

function EtfIdentity({ row }: { row: FundFlowAmountRow }) {
  const name = row.name?.trim()
  const code = row.code || ''
  return (
    <span className="min-w-0 truncate" data-etf-name title={name || undefined}>
      {name || code || '—'}
      {name && code ? <span className="ml-2 text-[11px] text-muted" data-etf-code>{code}</span> : null}
    </span>
  )
}

function etfFootnote(row: FundFlowAmountRow): string {
  const parts: string[] = []
  if (row.shares != null) parts.push(`份额 ${(row.shares / 1e8).toFixed(2)} 亿份`)
  const navigator = navigatorInflow(row)
  if (navigator) parts.push(navigator)
  if (row.flow_diverged) parts.push('与领航者相差超过 20%')
  return parts.join(' · ')
}

function EtfLine({
  row,
  baselines,
}: {
  row: FundFlowAmountRow
  baselines: Record<string, string | null> | undefined
}) {
  const footnote = etfFootnote(row)
  return (
    <li
      className={cn(
        'min-w-0',
        row.flow_diverged && 'rounded border border-warning/40 bg-warning/10 px-1.5 py-1',
      )}
      title={row.flow_diverged ? '推算净流入与领航者相差超过 20%' : undefined}
    >
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <EtfIdentity row={row} />
        <span className="flex shrink-0 gap-2 sm:gap-3">
          {ETF_COLUMNS.map(column => {
            const shares = column.shares(row)
            const inflow = column.inflow(row)
            return (
              <span
                key={column.days}
                data-window={column.days}
                className={cn(ETF_COL_WIDTH, 'inline-flex flex-col items-end gap-0.5')}
              >
                <span
                  data-kind="shares"
                  className={cn('whitespace-nowrap font-mono text-xs', signedClass(shares))}
                  title={etfBaselineTitle(baselines, column.days)}
                >
                  {formatShares(shares)}
                </span>
                <span
                  data-kind="inflow"
                  className={cn('whitespace-nowrap font-mono text-[11px]', signedClass(inflow))}
                >
                  {formatYiYuan(inflow)}
                </span>
              </span>
            )
          })}
        </span>
      </div>
      {footnote ? (
        <div className={cn('mt-0.5 text-[11px]', row.flow_diverged ? 'text-warning' : 'text-muted')}>
          {footnote}
        </div>
      ) : null}
    </li>
  )
}

function EtfGroupSection({
  label,
  items,
  expanded,
  onToggle,
  baselines,
  badge,
}: {
  label: string
  items: FundFlowAmountRow[]
  expanded: boolean
  onToggle: () => void
  baselines: Record<string, string | null> | undefined
  badge?: boolean
}) {
  const inflows = groupNetInflows(items)
  // 表头已经是 1日 / 5日 / 20日，列内不再写「N日合计」前缀：三列各占 5.75rem，
  // 带前缀会挤爆。任一窗口有合计才渲染整块，三列都缺时与「无合计」语义一致。
  const cells: { window: '1d' | '5d' | '20d'; value: number | null }[] = [
    { window: '1d', value: inflows.d1 },
    { window: '5d', value: inflows.d5 },
    { window: '20d', value: inflows.d20 },
  ]
  const hasInflow = cells.some(cell => cell.value != null)
  return (
    <div>
      <button
        type="button"
        className="sticky top-0 z-10 flex w-full items-center gap-1 bg-base py-1 text-left text-[11px] text-muted hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
        data-etf-group={label}
        data-expanded={expanded ? 'true' : 'false'}
        aria-expanded={expanded}
        title={expanded ? '收起这一组' : '展开这一组'}
        onClick={onToggle}
      >
        <ChevronRight
          className={cn('h-3 w-3 shrink-0 transition-transform', expanded && 'rotate-90')}
          aria-hidden
        />
        <span className="truncate">{label}</span>
        <span className="shrink-0" data-etf-group-count>{items.length} 只</span>
        {badge ? (
          <span className="ml-1 shrink-0 rounded border border-accent/40 px-1 py-0.5 text-[10px] text-accent">
            国家队代理
          </span>
        ) : null}
        {!expanded && hasInflow ? (
          <span className="ml-auto flex shrink-0 gap-2 sm:gap-3">
            {cells.map(cell => (
              <span
                key={cell.window}
                className={cn(
                  ETF_COL_WIDTH,
                  'text-right font-mono whitespace-nowrap',
                  cell.value != null && signedClass(cell.value),
                )}
                data-etf-group-inflow
                data-window={cell.window}
              >
                {cell.value != null ? formatYiYuan(cell.value) : ''}
              </span>
            ))}
          </span>
        ) : null}
      </button>
      {expanded ? (
        <ul className="space-y-2">
          {items.map(row => (
            <EtfLine key={row.code} row={row} baselines={baselines} />
          ))}
        </ul>
      ) : null}
    </div>
  )
}

function EtfShareList({
  rows,
  baselines,
}: {
  rows: FundFlowAmountRow[]
  baselines: Record<string, string | null> | undefined
}) {
  const { groups, others } = groupEtfRows(rows)
  const [collapsed, setCollapsed] = useState(readCollapsedEtfGroups)

  function toggle(label: string) {
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(label)) next.delete(label)
      else next.add(label)
      writeCollapsedEtfGroups(next)
      return next
    })
  }

  return (
    <div className="min-w-0 overflow-x-auto">
      <p className="mb-2 text-[11px] leading-relaxed text-muted">
        每列上方是份额变化，下方是净流入。净流入 = (当日份额 − 上一份落盘) × 收盘价，单位亿元；5 日 / 20 日是相邻交易日的滚动合计。复权跳变或中间缺交易日时，净流入留空。份额变化按当前份额口径折算：窗口跨过已确认的登记日时，基准份额乘上其后的折算倍数，拆分或缩股本身不计入变化。
      </p>
      <div className="mb-1 flex items-baseline justify-between gap-3 text-[11px] text-muted">
        <span>名称</span>
        <span className="flex shrink-0 gap-2 sm:gap-3" data-etf-windows>
          {ETF_COLUMNS.map(column => (
            <span key={column.days} className={cn(ETF_COL_WIDTH, 'text-right')}>
              {column.label}
            </span>
          ))}
        </span>
      </div>
      <div className="max-h-80 space-y-2 overflow-y-auto pr-1" data-etf-list>
        {groups.map(group => (
          <EtfGroupSection
            key={group.label}
            label={group.label}
            items={group.items}
            expanded={!collapsed.has(group.label)}
            onToggle={() => toggle(group.label)}
            baselines={baselines}
            badge={group.label === '沪深300' || group.label === '科创50'}
          />
        ))}
        {others.length > 0 ? (
          <EtfGroupSection
            label="其他"
            items={others}
            expanded={!collapsed.has('其他')}
            onToggle={() => toggle('其他')}
            baselines={baselines}
          />
        ) : null}
      </div>
    </div>
  )
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
                <EtfShareList rows={etf?.items ?? []} baselines={etf?.baselines} />
              )}
            </Card>
          </div>
        )}
      </div>
    </div>
  )
}
