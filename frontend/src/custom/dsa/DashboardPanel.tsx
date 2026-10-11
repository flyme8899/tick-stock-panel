import { useEffect, useMemo, useRef, useState, type ReactNode, type TouchEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MoreHorizontal } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { dsaUpstream, isRecord } from './client'
import {
  dedupeMarketReviewCards,
  formatPercentPoints,
  isMarketReview,
  meaningfulStockName,
  nameLookupSymbols,
  namesByWatchKey,
  poolSnapshot,
  resolvePoolName,
  toDashboardCard,
  watchKey,
  type DashboardCard,
} from './dashboardHistory'
import { SAMPLE_HISTORY, SAMPLE_WATCH_POOL } from './samples'

const PAGE_SIZE = 40
const ghostBtn = 'inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-3 text-xs text-secondary hover:text-foreground disabled:opacity-50'
const dangerBtn = 'inline-flex h-8 items-center gap-1.5 rounded-btn bg-danger px-3 text-xs text-white disabled:opacity-50'
const fieldCls = 'h-8 rounded-btn border border-border bg-base px-2.5 text-xs text-foreground outline-none focus:border-accent'

export interface WatchPoolItem {
  code: string
  name: string
  price?: number | null
  /** 百分数口径，1.23 表示 +1.23%。 */
  changePctPoints?: number | null
}

interface ConfirmState {
  title: string
  body: string
  confirmLabel: string
  run: () => void
}

function adviceClass(advice: string) {
  if (/买/.test(advice)) return 'text-bull'
  if (/卖/.test(advice)) return 'text-bear'
  if (/观望|持有|复盘/.test(advice)) return 'text-warning'
  return 'text-secondary'
}

function historyPage(data: unknown): { rows: Record<string, unknown>[]; total: number } {
  if (!isRecord(data)) return { rows: [], total: 0 }
  const items = Array.isArray(data.items) ? data.items.filter(isRecord) : []
  const total = typeof data.total === 'number' ? data.total : items.length
  return { rows: items, total }
}

export function DashboardPanel({
  sample,
  reachable,
  review,
  footer,
}: {
  sample: boolean
  reachable: boolean
  review?: ReactNode
  footer?: ReactNode
}) {
  const qc = useQueryClient()
  const [page, setPage] = useState(1)
  const [hiddenIds, setHiddenIds] = useState<number[]>([])
  const [readIds, setReadIds] = useState<number[]>([])
  const [localPool, setLocalPool] = useState<string[]>(SAMPLE_WATCH_POOL)
  const history = useQuery({
    queryKey: [...QK.dsaUpstream('history'), 'dashboard', page],
    queryFn: () => dsaUpstream<unknown>(
      `history?limit=${PAGE_SIZE}&page=${page}&dedupe_market_review=true`,
    ),
    enabled: reachable,
    retry: false,
  })
  const watch = useQuery({
    queryKey: [...QK.dsaUpstream('stocks/watchlist'), 'pool'],
    queryFn: () => dsaUpstream<{ stock_codes?: unknown }>('stocks/watchlist'),
    enabled: reachable,
    retry: false,
  })
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: QK.dsaUpstream('history') })
    qc.invalidateQueries({ queryKey: QK.dsaUpstream('stocks/watchlist') })
    qc.invalidateQueries({ queryKey: QK.dsaSchedule })
  }
  const dismiss = useMutation({
    mutationFn: (body: Record<string, unknown>) => dsaUpstream<{ dismissed: number }>('history/dismiss', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
    onSuccess: () => {
      setPage(1)
      invalidate()
    },
  })
  const markRead = useMutation({
    mutationFn: (ids: number[]) => dsaUpstream<{ updated: number }>('history/read', {
      method: 'POST',
      body: JSON.stringify({ record_ids: ids }),
    }),
    onSuccess: invalidate,
  })
  const removePool = useMutation({
    mutationFn: (codes: string[]) => dsaUpstream<{ stock_codes: string[] }>('stocks/watchlist/remove-batch', {
      method: 'POST',
      body: JSON.stringify({ stock_codes: codes }),
    }),
    onSuccess: invalidate,
  })

  const sampleRows = SAMPLE_HISTORY.map(row => ({ ...row }) as Record<string, unknown>)
  const loaded = reachable ? historyPage(history.data) : { rows: sample ? sampleRows : [], total: sample ? sampleRows.length : 0 }
  const cards = useMemo(() => {
    const visible = dedupeMarketReviewCards(loaded.rows.map(toDashboardCard))
      .filter(card => card.id == null || !hiddenIds.includes(card.id))
      .map(card => (card.id != null && readIds.includes(card.id) ? { ...card, read: true } : card))
    return visible
  }, [loaded.rows, hiddenIds, readIds])
  const remotePool = Array.isArray(watch.data?.stock_codes)
    ? watch.data.stock_codes.filter((code): code is string => typeof code === 'string' && code.trim() !== '')
    : []
  const poolCodes = reachable ? remotePool : sample ? localPool : []
  const lookupKey = [...new Set(poolCodes.flatMap(nameLookupSymbols))].sort().join(',')
  const tspWatch = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    staleTime: 60_000,
    retry: false,
  })
  const instrumentNames = useQuery({
    queryKey: QK.instrumentNames(lookupKey),
    queryFn: () => api.instrumentNames(lookupKey.split(',')),
    enabled: lookupKey !== '',
    staleTime: 300_000,
    retry: false,
  })
  const nameByKey = useMemo(() => {
    const merged: Record<string, string | null | undefined> = {}
    for (const entry of tspWatch.data?.symbols ?? []) {
      if (entry.symbol && entry.name) merged[entry.symbol] = entry.name
    }
    Object.assign(merged, instrumentNames.data?.names ?? {})
    return namesByWatchKey(merged)
  }, [tspWatch.data, instrumentNames.data])
  const namedCards = useMemo(() => cards.map(card => {
    if (meaningfulStockName(card.stockName, card.stockCode)) return card
    const mapped = nameByKey.get(watchKey(card.stockCode))
    return mapped ? { ...card, stockName: mapped } : card
  }), [cards, nameByKey])
  const pool = poolCodes.map(code => {
    const snapshot = poolSnapshot(code, namedCards)
    return {
      code,
      name: resolvePoolName(code, namedCards, nameByKey),
      price: snapshot.price,
      changePctPoints: snapshot.changePctPoints,
    }
  })
  const error = history.error ?? watch.error ?? dismiss.error ?? markRead.error ?? removePool.error
  const busy = dismiss.isPending || markRead.isPending || removePool.isPending

  return (
    <div className="space-y-3">
      <DashboardBoard
        rows={namedCards}
        total={reachable ? loaded.total : cards.length}
        page={page}
        pageSize={PAGE_SIZE}
        pool={pool}
        sample={sample}
        reachable={reachable}
        busy={busy}
        errorText={error instanceof Error ? error.message : ''}
        review={review}
        onPage={setPage}
        onRetry={() => {
          history.refetch()
          watch.refetch()
        }}
        onDismiss={ids => {
          if (!reachable) {
            setHiddenIds(current => [...current, ...ids])
            return
          }
          dismiss.mutate({ record_ids: ids })
        }}
        onDismissRead={() => {
          if (!reachable) {
            setHiddenIds(current => [...current, ...cards.filter(card => card.read && card.id != null).map(card => card.id as number)])
            return
          }
          dismiss.mutate({ only_read: true })
        }}
        onDismissOlder={days => {
          if (!reachable) {
            const cutoff = Date.now() - days * 24 * 60 * 60 * 1000
            setHiddenIds(current => [
              ...current,
              ...cards.filter(card => {
                const parsed = Date.parse(card.createdAt.replace(' ', 'T'))
                return card.id != null && !Number.isNaN(parsed) && parsed < cutoff
              }).map(card => card.id as number),
            ])
            return
          }
          dismiss.mutate({ older_than_days: days })
        }}
        onMarkRead={ids => {
          if (!reachable) {
            setReadIds(current => [...current, ...ids])
            return
          }
          markRead.mutate(ids)
        }}
        onRemovePool={codes => {
          if (!reachable) {
            const keys = new Set(codes.map(watchKey))
            setLocalPool(current => current.filter(code => !keys.has(watchKey(code))))
            return
          }
          removePool.mutate(codes)
        }}
      />
      {footer}
    </div>
  )
}

export function DashboardBoard({
  rows,
  total,
  page,
  pageSize,
  pool,
  sample,
  reachable,
  busy,
  errorText,
  review,
  onPage,
  onRetry,
  onDismiss,
  onDismissRead,
  onDismissOlder,
  onMarkRead,
  onRemovePool,
}: {
  rows: DashboardCard[]
  total: number
  page: number
  pageSize: number
  pool: WatchPoolItem[]
  sample: boolean
  reachable: boolean
  busy: boolean
  errorText: string
  review?: ReactNode
  onPage: (page: number) => void
  onRetry: () => void
  onDismiss: (ids: number[]) => void
  onDismissRead: () => void
  onDismissOlder: (days: number) => void
  onMarkRead: (ids: number[]) => void
  onRemovePool: (codes: string[]) => void
}) {
  const [selectingCards, setSelectingCards] = useState(false)
  const [selectedCards, setSelectedCards] = useState<number[]>([])
  const [selectingPool, setSelectingPool] = useState(false)
  const [selectedPool, setSelectedPool] = useState<string[]>([])
  const [days, setDays] = useState('7')
  const [confirm, setConfirm] = useState<ConfirmState | null>(null)
  const counts = useMemo(() => {
    const bucket = { buy: 0, hold: 0, sell: 0 }
    for (const row of rows) {
      if (/买/.test(row.advice)) bucket.buy += 1
      else if (/卖/.test(row.advice)) bucket.sell += 1
      else bucket.hold += 1
    }
    return bucket
  }, [rows])
  const tracked = useMemo(() => new Set(pool.map(item => watchKey(item.code))), [pool])
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const ask = (state: ConfirmState) => setConfirm(state)

  const toggleCard = (id: number) => {
    setSelectedCards(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id])
  }
  const togglePool = (code: string) => {
    setSelectedPool(current => current.includes(code) ? current.filter(item => item !== code) : [...current, code])
  }

  return (
    <div className="space-y-3">
      <div data-decision-counts className="flex divide-x divide-border overflow-hidden rounded-lg border border-border bg-surface">
        <Stat label="买入" value={counts.buy} tone="text-bull" />
        <Stat label="观望" value={counts.hold} tone="text-warning" />
        <Stat label="卖出" value={counts.sell} tone="text-bear" />
      </div>
      <section className="rounded-lg border border-border bg-surface">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <h2 className="text-sm font-medium text-foreground">观察池{sample ? ' · 样例' : ''}</h2>
            <p className="mt-0.5 text-[11px] leading-relaxed text-muted">定时任务只分析这里的股票。移出后不再生成新的分析，已经出现的卡片还在下面。</p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <button type="button" className={ghostBtn} onClick={() => {
              setSelectingPool(value => !value)
              setSelectedPool([])
            }}>{selectingPool ? '取消多选' : '多选'}</button>
            {selectingPool && (
              <button
                type="button"
                className={dangerBtn}
                disabled={busy || selectedPool.length === 0}
                onClick={() => ask({
                  title: '移出观察池',
                  body: `把选中的 ${selectedPool.length} 只移出观察池？之后的定时分析不再包含它们。已有卡片不会自动删除。`,
                  confirmLabel: '移出',
                  run: () => {
                    onRemovePool(selectedPool)
                    setSelectedPool([])
                    setSelectingPool(false)
                  },
                })}
              >移出所选</button>
            )}
          </div>
        </header>
        <div className="px-4 py-3">
          {pool.length === 0 && <p className="rounded-btn bg-elevated px-3 py-2 text-xs text-secondary">{reachable || sample ? '观察池是空的。到「定时推送」写入自选列表后，每日分析会回到这里。' : '决策服务未连接。'}</p>}
          <ul className="grid gap-2 sm:grid-cols-2">
            {pool.map(item => {
              const title = meaningfulStockName(item.name, item.code)
              const label = title ?? item.code
              const quoteTone = item.changePctPoints == null ? 'text-foreground' : priceColorClass(item.changePctPoints)
              const hasQuote = item.price != null || item.changePctPoints != null
              return (
                <li key={item.code} className="flex items-center gap-2 rounded-lg border border-border bg-base px-3 py-2">
                  {selectingPool && (
                    <input
                      type="checkbox"
                      className="h-4 w-4 shrink-0"
                      checked={selectedPool.includes(item.code)}
                      aria-label={`选择 ${label}`}
                      onChange={() => togglePool(item.code)}
                    />
                  )}
                  <div className="min-w-0 flex-1">
                    <div data-pool-name className={cn('truncate text-sm text-foreground', title ? 'font-semibold' : 'font-mono')}>{label}</div>
                    {title && <div data-pool-code className="truncate font-mono text-[11px] text-muted">{item.code}</div>}
                  </div>
                  {hasQuote && (
                    <div data-pool-quote className={cn('num shrink-0 text-right text-xs leading-4', quoteTone)}>
                      {item.price != null && <div>{item.price.toFixed(2)}</div>}
                      {item.changePctPoints != null && <div>{formatPercentPoints(item.changePctPoints)}</div>}
                    </div>
                  )}
                  <OverflowMenu
                    label={`${label} 更多操作`}
                    items={[{
                      label: '移出观察池',
                      danger: true,
                      onSelect: () => ask({
                        title: '移出观察池',
                        body: `把「${label}」移出观察池？之后的定时分析不再包含它。已有卡片不会自动删除。`,
                        confirmLabel: '移出',
                        run: () => onRemovePool([item.code]),
                      }),
                    }]}
                  />
                </li>
              )
            })}
          </ul>
        </div>
      </section>
      <section className="rounded-lg border border-border bg-surface">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <h2 className="text-sm font-medium text-foreground">{sample ? '今日决策 · 样例' : '今日决策'}</h2>
            <p className="mt-0.5 text-[11px] leading-relaxed text-muted">同一天的大盘复盘只保留最新一条。移出后仪表盘不再显示，原记录仍留在决策服务里。</p>
          </div>
          {review}
        </header>
        <div className="space-y-3 px-4 py-3">
          <div className="flex flex-wrap items-center gap-1.5">
            <button type="button" className={ghostBtn} onClick={() => {
              setSelectingCards(value => !value)
              setSelectedCards([])
            }}>{selectingCards ? '取消多选' : '多选删除'}</button>
            {selectingCards && (
              <>
                <button type="button" className={ghostBtn} onClick={() => setSelectedCards(rows.flatMap(row => row.id == null ? [] : [row.id]))}>全选本页</button>
                <button
                  type="button"
                  className={dangerBtn}
                  disabled={busy || selectedCards.length === 0}
                  onClick={() => ask({
                    title: '删除所选分析',
                    body: `移出选中的 ${selectedCards.length} 条分析？仪表盘不再显示。`,
                    confirmLabel: '删除',
                    run: () => {
                      onDismiss(selectedCards)
                      setSelectedCards([])
                      setSelectingCards(false)
                    },
                  })}
                >删除所选</button>
              </>
            )}
            <button
              type="button"
              className={ghostBtn}
              disabled={busy || (!reachable && rows.every(row => !row.read))}
              onClick={() => ask({
                title: '清空已读',
                body: '移出全部已标为已读的分析？未读的会保留。不只限于当前这一页。',
                confirmLabel: '清空已读',
                run: onDismissRead,
              })}
            >清空已读</button>
            <label className="inline-flex items-center gap-1.5 text-[11px] text-muted">
              旧于
              <input
                className={cn(fieldCls, 'w-16 font-mono')}
                value={days}
                inputMode="numeric"
                aria-label="天数"
                onChange={event => setDays(event.target.value.replace(/[^\d]/g, '').slice(0, 4))}
              />
              天
              <button
                type="button"
                className={ghostBtn}
                disabled={busy || !/^\d+$/.test(days) || Number(days) < 1}
                onClick={() => ask({
                  title: '清除较早的分析',
                  body: `移出早于 ${Number(days)} 天的分析？天数按决策服务的时钟计算。`,
                  confirmLabel: '清除',
                  run: () => onDismissOlder(Number(days)),
                })}
              >清除</button>
            </label>
          </div>
          {errorText && (
            <div className="flex items-center justify-between gap-3 rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-secondary">
              <span>{errorText}</span>
              <button type="button" className={ghostBtn} onClick={onRetry}>重试</button>
            </div>
          )}
          {rows.length === 0 && !errorText && (
            <p className="rounded-btn bg-elevated px-3 py-2 text-xs text-secondary">
              {reachable || sample ? '还没有分析记录。到「个股研报」提交一只股票，或等定时任务跑完。' : '决策服务未连接。'}
            </p>
          )}
          <div className="grid gap-2 lg:grid-cols-2">
            {rows.map(row => {
              const poolCode = pool.find(item => watchKey(item.code) === watchKey(row.stockCode))?.code
              return (
                <AnalysisCard
                  key={row.id ?? `${row.stockCode}-${row.createdAt}`}
                  row={row}
                  selecting={selectingCards}
                  selected={row.id != null && selectedCards.includes(row.id)}
                  onToggle={() => row.id != null && toggleCard(row.id)}
                  onDelete={() => row.id != null && ask({
                    title: '删除这条分析',
                    body: `移出「${row.stockName}」这条分析？仪表盘不再显示。`,
                    confirmLabel: '删除',
                    run: () => onDismiss([row.id as number]),
                  })}
                  onRead={() => row.id != null && onMarkRead([row.id])}
                  onUntrack={poolCode ? () => ask({
                    title: '移出观察池',
                    body: `把「${row.stockName}」移出观察池？之后的定时分析不再包含它。这条卡片还在，可以另外删除。`,
                    confirmLabel: '移出',
                    run: () => onRemovePool([poolCode]),
                  }) : undefined}
                  tracked={!isMarketReview(row) && tracked.has(watchKey(row.stockCode))}
                />
              )
            })}
          </div>
          {total > pageSize && (
            <div className="flex items-center justify-between text-[11px] text-muted">
              <span>第 {page} / {pageCount} 页，共 {total} 条</span>
              <div className="flex gap-1.5">
                <button type="button" className={ghostBtn} disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
                <button type="button" className={ghostBtn} disabled={page >= pageCount} onClick={() => onPage(page + 1)}>更早的记录</button>
              </div>
            </div>
          )}
        </div>
      </section>
      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          body={confirm.body}
          confirmLabel={confirm.confirmLabel}
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            confirm.run()
            setConfirm(null)
          }}
        />
      )}
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="flex min-w-0 flex-1 items-baseline justify-center gap-1.5 px-2 py-2">
      <span className="truncate text-[11px] text-muted">{label}</span>
      <span className={cn('num text-lg leading-none', tone)}>{value}</span>
    </div>
  )
}

function AnalysisCard({
  row,
  selecting,
  selected,
  tracked,
  onToggle,
  onDelete,
  onRead,
  onUntrack,
}: {
  row: DashboardCard
  selecting: boolean
  selected: boolean
  tracked: boolean
  onToggle: () => void
  onDelete: () => void
  onRead: () => void
  onUntrack?: () => void
}) {
  const [open, setOpen] = useState(false)
  const startX = useRef<number | null>(null)
  const onTouchStart = (event: TouchEvent) => {
    startX.current = event.changedTouches[0]?.clientX ?? null
  }
  const onTouchEnd = (event: TouchEvent) => {
    if (startX.current == null || selecting) return
    const dx = (event.changedTouches[0]?.clientX ?? startX.current) - startX.current
    if (dx <= -48) setOpen(true)
    else if (dx >= 24) setOpen(false)
    startX.current = null
  }
  const title = meaningfulStockName(row.stockName, row.stockCode)
  const menu = [
    ...(!row.read && row.id != null ? [{ label: '标为已读', onSelect: onRead }] : []),
    ...(tracked && onUntrack ? [{ label: '移出观察池', danger: true, onSelect: onUntrack }] : []),
    ...(row.id != null ? [{ label: '删除', danger: true, onSelect: onDelete }] : []),
  ]
  return (
    <div className="relative overflow-hidden rounded-lg">
      <button
        type="button"
        className="absolute inset-y-0 right-0 w-16 bg-danger text-xs text-white"
        onClick={onDelete}
        tabIndex={open ? 0 : -1}
      >删除</button>
      <article
        className={cn('relative border border-border bg-base px-3 py-2.5 transition-transform', open && !selecting && '-translate-x-16')}
        onTouchStart={onTouchStart}
        onTouchEnd={onTouchEnd}
      >
        <div className="flex items-start gap-2">
          {selecting && (
            <input
              type="checkbox"
              className="mt-1 h-4 w-4"
              checked={selected}
              disabled={row.id == null}
              aria-label={`选择 ${row.stockName}`}
              onChange={onToggle}
            />
          )}
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-2">
              <div className="min-w-0">
                {title ? (
                  <>
                    <span className="text-sm font-semibold text-foreground">{title}</span>
                    <span className="ml-2 font-mono text-[11px] text-muted">{row.stockCode}</span>
                  </>
                ) : (
                  <span className="font-mono text-sm text-foreground">{row.stockCode}</span>
                )}
                {row.read && <span className="ml-2 text-[10px] text-muted">已读</span>}
              </div>
              <span className={cn('shrink-0 text-xs font-medium', adviceClass(row.advice))}>{row.advice || '—'}</span>
            </div>
            <div className="mt-1 flex gap-3 text-[11px] text-muted">
              <span>评分 <b className="num text-foreground">{row.score || '—'}</b></span>
              <span>{row.trend}</span>
              {row.region && <span className="uppercase">{row.region}</span>}
            </div>
            <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-secondary">{row.summary}</p>
          </div>
          <OverflowMenu label={`${row.stockName} 更多操作`} items={menu} />
        </div>
      </article>
    </div>
  )
}

function OverflowMenu({
  label,
  items,
}: {
  label: string
  items: { label: string; onSelect: () => void; danger?: boolean }[]
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])
  if (items.length === 0) return null
  return (
    <div ref={ref} className="relative shrink-0">
      <button
        type="button"
        className="inline-flex h-11 w-11 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground"
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen(value => !value)}
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 z-20 mt-1 min-w-36 rounded-lg border border-border bg-surface py-1 shadow-lg">
          {items.map(item => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              className={cn(
                'block min-h-11 w-full px-3 text-left text-xs hover:bg-elevated',
                item.danger ? 'text-danger' : 'text-foreground',
              )}
              onClick={() => {
                setOpen(false)
                item.onSelect()
              }}
            >{item.label}</button>
          ))}
        </div>
      )}
    </div>
  )
}

function ConfirmDialog({
  title,
  body,
  confirmLabel,
  busy,
  onCancel,
  onConfirm,
}: {
  title: string
  body: string
  confirmLabel: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-4 sm:items-center" role="dialog" aria-modal="true" aria-labelledby="dashboard-confirm-title">
      <div className="w-full max-w-sm rounded-lg border border-border bg-surface p-4 shadow-xl">
        <h3 id="dashboard-confirm-title" className="text-sm font-medium text-foreground">{title}</h3>
        <p className="mt-2 text-xs leading-relaxed text-secondary">{body}</p>
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" className={ghostBtn} onClick={onCancel}>取消</button>
          <button type="button" className={dangerBtn} disabled={busy} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  )
}
