import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Download, FlaskConical, Loader2, Play, Star, X } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { SourceDropdown } from '@/components/picker/SourceDropdown'
import { WatchlistAddMenu } from '@/components/WatchlistAddMenu'
import { toast } from '@/components/Toast'
import { api, type PickerRow, type PickerRunResponse, type PickerSourceGroup } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { toNavItems } from '@/lib/listNav'
import { QK } from '@/lib/queryKeys'

type Pick = { type: string; id: string }

const STRATEGY_TAG = 'inline-block rounded border border-amber-500/20 bg-amber-500/10 px-1.5 py-px text-[10px] font-medium leading-tight text-amber-600 dark:text-amber-400'

function optionalNumber(raw: string): number | undefined {
  const text = raw.trim()
  if (!text) return undefined
  const value = Number(text)
  return Number.isFinite(value) ? value : undefined
}

function fmtRatioPercent(value: number | null): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value.toFixed(1)}%`
}

function idsOf(picks: Pick[], type: string): string[] {
  return picks.filter(item => item.type === type).map(item => item.id)
}

function togglePick(picks: Pick[], type: string, id: string): Pick[] {
  const exists = picks.some(item => item.type === type && item.id === id)
  return exists ? picks.filter(item => !(item.type === type && item.id === id)) : [...picks, { type, id }]
}

function exportCsv(rows: PickerRow[], profitLabel: string) {
  const header = ['代码', '名称', '行业', '得分', '命中策略', '事件', 'ROE', profitLabel, 'PE', '市值', '变动']
  const lines = rows.map(row => [
    row.symbol,
    row.name,
    row.industry,
    row.score ?? '',
    row.strategies.map(item => item.name).join(' '),
    row.event,
    row.roe ?? '',
    row.profit_yoy ?? '',
    row.pe ?? '',
    row.market_cap ?? '',
    row.change === 'new' ? '新进' : '保留',
  ].map(value => `"${String(value).replace(/"/g, '""')}"`).join(','))
  const blob = new Blob([`\uFEFF${[header.join(','), ...lines].join('\n')}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'stock-picker.csv'
  link.click()
  URL.revokeObjectURL(url)
}

function groupById(groups: PickerSourceGroup[] | undefined, id: string): PickerSourceGroup | undefined {
  return groups?.find(group => group.id === id)
}

export function StockPicker() {
  const navigate = useNavigate()
  const sources = useQuery({
    queryKey: QK.pickerSources,
    queryFn: api.pickerSources,
    staleTime: 30_000,
  })
  const [picks, setPicks] = useState<Pick[]>([])
  const [combine, setCombine] = useState<'and' | 'or'>('or')
  const [industries, setIndustries] = useState<string[]>([])
  const [capMin, setCapMin] = useState('')
  const [capMax, setCapMax] = useState('')
  const [peMin, setPeMin] = useState('')
  const [peMax, setPeMax] = useState('')
  const [excludeSt, setExcludeSt] = useState(false)
  const [excludeFinancial, setExcludeFinancial] = useState(false)
  const [maxPerIndustry, setMaxPerIndustry] = useState('')
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [syncDsa, setSyncDsa] = useState(false)
  const [scheduleNote, setScheduleNote] = useState(false)
  const [result, setResult] = useState<PickerRunResponse | null>(null)
  const [preview, setPreview] = useState<string | null>(null)

  const groups = sources.data?.groups
  const names = useMemo(() => {
    const map = new Map<string, string>()
    for (const group of groups ?? []) {
      for (const item of group.items) map.set(`${group.id}:${item.id}`, item.name)
    }
    return map
  }, [groups])

  const run = useMutation({
    mutationFn: () => {
      return api.pickerRun({
        sources: picks.map(item => ({
          type: item.type,
          id: item.id,
        })),
        combine,
        filters: {
          industries,
          market_cap_min: optionalNumber(capMin),
          market_cap_max: optionalNumber(capMax),
          pe_min: optionalNumber(peMin),
          pe_max: optionalNumber(peMax),
          exclude_st: excludeSt,
          exclude_financial: excludeFinancial,
          max_per_industry: (() => {
            const value = optionalNumber(maxPerIndustry)
            return value == null ? undefined : Math.max(1, Math.trunc(value))
          })(),
        },
      })
    },
    onSuccess: data => {
      setResult(data)
      setChecked(new Set())
    },
    onError: (error: Error) => toast(error.message || '选股运行失败', 'error'),
  })

  const addWatchlist = useMutation({
    mutationFn: async (groupId: string | null) => {
      const symbols = actionSymbols()
      const added = await api.watchlistBatchAdd(symbols, '', groupId)
      if (syncDsa) {
        const synced = await api.pickerDsaSync(symbols)
        return { added: added.added, dsa: synced.message }
      }
      return { added: added.added, dsa: '' }
    },
    onSuccess: payload => {
      toast(payload.dsa ? `已加入自选 ${payload.added} 只。${payload.dsa}` : `已加入自选 ${payload.added} 只`, payload.dsa && payload.dsa.includes('失败') ? 'error' : 'success')
    },
    onError: (error: Error) => toast(error.message || '加入自选失败', 'error'),
  })

  const rows = result?.rows ?? []
  const summary = result?.summary
  const profitLabel = summary?.profit_yoy_label ?? '扣非增速'
  const hot = groupById(groups, 'hot_events')
  const dsa = groupById(groups, 'dsa')
  const factor = groupById(groups, 'factor')

  function actionSymbols(): string[] {
    const pool = checked.size ? rows.filter(row => checked.has(row.symbol)) : rows
    return pool.map(row => row.symbol)
  }

  function submit() {
    if (!picks.length) {
      toast('先选择至少一个来源', 'error')
      return
    }
    const capLo = optionalNumber(capMin)
    const capHi = optionalNumber(capMax)
    const peLo = optionalNumber(peMin)
    const peHi = optionalNumber(peMax)
    if (capLo != null && capHi != null && capLo > capHi) {
      toast('市值下限不能大于上限', 'error')
      return
    }
    if (peLo != null && peHi != null && peLo > peHi) {
      toast('市盈率下限不能大于上限', 'error')
      return
    }
    run.mutate()
  }

  const allChecked = rows.length > 0 && rows.every(row => checked.has(row.symbol))

  return (
    <div className="flex min-h-full flex-col bg-base">
      <PageHeader
        title="选股"
        subtitle="多个来源取交集或并集，结果和上一份快照对比"
        className="bg-base/95"
      />
      <div className="flex flex-col gap-3 p-3 md:p-4">
        <div className="grid grid-cols-1 gap-2 md:grid-cols-5">
          <SourceDropdown
            label="基本面"
            options={groupById(groups, 'fundamental')?.items ?? []}
            selectedIds={idsOf(picks, 'fundamental')}
            onToggle={id => setPicks(current => togglePick(current, 'fundamental', id))}
          />
          <SourceDropdown
            label="热门事件"
            searchable={false}
            options={(hot?.items ?? []).map(item => ({
              id: item.id,
              name: item.name,
              mentions: item.mentions,
              sourceCount: item.source_count,
              updatedAt: item.first_seen ?? item.updated_at ?? undefined,
              concepts: item.concepts,
              headline: item.headline ?? undefined,
            }))}
            selectedIds={idsOf(picks, 'hot_events')}
            onToggle={id => setPicks(current => togglePick(current, 'hot_events', id))}
            hint={hot?.hint || undefined}
            hintClassName={hot?.fallback ? 'text-amber-600 dark:text-amber-400' : undefined}
            empty={sources.isError ? (
              <p className="px-2 py-3 text-xs text-danger">来源列表加载失败</p>
            ) : hot?.error ? (
              <p className="px-2 py-3 text-xs text-danger">{hot.error}</p>
            ) : sources.isPending ? (
              <p className="px-2 py-3 text-xs text-muted">加载中…</p>
            ) : (
              <p className="px-2 py-3 text-xs text-muted">今日暂无热门事件</p>
            )}
          />
          <SourceDropdown
            label="技术面/短线"
            options={groupById(groups, 'technical')?.items ?? []}
            selectedIds={idsOf(picks, 'technical')}
            onToggle={id => setPicks(current => togglePick(current, 'technical', id))}
          />
          <SourceDropdown
            label="因子打分"
            options={factor?.items ?? []}
            selectedIds={idsOf(picks, 'factor')}
            onToggle={id => setPicks(current => togglePick(current, 'factor', id))}
            empty={factor && factor.items.length === 0 ? (
              <p className="px-2 py-3 text-xs text-muted">
                还没有因子生成的策略。
                <Link to="/factors" className="ml-1 text-accent">去因子页生成</Link>
              </p>
            ) : undefined}
          />
          <SourceDropdown
            label="DSA 选股"
            badge="beta"
            options={dsa?.available ? dsa.items : []}
            selectedIds={idsOf(picks, 'dsa')}
            onToggle={id => setPicks(current => togglePick(current, 'dsa', id))}
            empty={!dsa?.available ? (
              <p className="px-2 py-3 text-xs text-muted">{dsa?.error || '决策服务未连接'}</p>
            ) : undefined}
          />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {picks.map(item => (
            <button
              key={`${item.type}:${item.id}`}
              type="button"
              onClick={() => setPicks(current => current.filter(pick => !(pick.type === item.type && pick.id === item.id)))}
              className="inline-flex max-w-full items-center gap-1 rounded-full border border-border bg-elevated px-2 py-1 text-[11px] text-foreground"
            >
              <span className="truncate">{names.get(`${item.type}:${item.id}`) || item.id}</span>
              <X className="h-3 w-3 text-muted" />
            </button>
          ))}
          <div className="inline-flex rounded-btn border border-border p-0.5">
            {([['or', '并集'], ['and', '交集']] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setCombine(value)}
                className={cn(
                  'rounded px-2 py-1 text-[11px]',
                  combine === value ? 'bg-accent text-white' : 'text-muted',
                )}
              >
                {label}
              </button>
            ))}
          </div>
          <SourceDropdown
            label={industries.length ? `行业 ${industries.length}` : '行业'}
            options={(sources.data?.industries ?? []).map(name => ({ id: name, name }))}
            selectedIds={industries}
            onToggle={id => setIndustries(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id])}
          />
          <RangeInput label="市值亿" min={capMin} max={capMax} onMin={setCapMin} onMax={setCapMax} />
          <RangeInput label="PE" min={peMin} max={peMax} onMin={setPeMin} onMax={setPeMax} />
          <CheckFilter label="排除ST" checked={excludeSt} onChange={setExcludeSt} />
          <CheckFilter label="排除金融" checked={excludeFinancial} onChange={setExcludeFinancial} />
          <label className="inline-flex items-center gap-1 text-[11px] text-muted">
            每行业最多
            <input
              value={maxPerIndustry}
              onChange={event => setMaxPerIndustry(event.target.value)}
              inputMode="numeric"
              className="h-8 w-12 rounded-input border border-border bg-surface px-1.5 text-xs text-foreground"
            />
          </label>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={submit}
              disabled={run.isPending}
              className="inline-flex h-8 items-center gap-1 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-50"
            >
              {run.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              运行
            </button>
            <button
              type="button"
              disabled={!rows.length}
              onClick={() => exportCsv(rows, profitLabel)}
              className="inline-flex h-8 items-center gap-1 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary disabled:opacity-40"
            >
              <Download className="h-3.5 w-3.5" />
              导出CSV
            </button>
            <WatchlistAddMenu
              disabled={!rows.length || addWatchlist.isPending}
              onSelect={groupId => addWatchlist.mutate(groupId)}
              title="批量加自选"
            >
              <span className="inline-flex h-8 items-center gap-1 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary">
                <Star className="h-3.5 w-3.5" />
                批量加自选
              </span>
            </WatchlistAddMenu>
            <CheckFilter label="同步DSA" checked={syncDsa} onChange={setSyncDsa} />
            <button
              type="button"
              disabled={!rows.length}
              onClick={() => {
                const symbols = actionSymbols()
                const strategy = picks.find(item => item.type === 'technical' || item.type === 'factor')?.id
                const params = new URLSearchParams({ symbols: symbols.join(',') })
                if (strategy) params.set('strategy', strategy)
                navigate(`/backtest?${params.toString()}`)
              }}
              className="inline-flex h-8 items-center gap-1 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary disabled:opacity-40"
            >
              <FlaskConical className="h-3.5 w-3.5" />
              一键回测
            </button>
            <button
              type="button"
              onClick={() => setScheduleNote(true)}
              className="inline-flex h-8 items-center rounded-btn border border-dashed border-border px-2.5 text-xs text-muted"
            >
              存为定时任务
            </button>
          </div>
        </div>
        {scheduleNote && (
          <p className="text-[11px] text-muted">
            定时任务下一期再做。到时候会保存当前来源、交集/并集和过滤条件，按交易日自动跑。这一期请先手动运行。
          </p>
        )}

        <p className="text-xs text-secondary">
          {summary ? (
            <>
              共 {summary.total} 只 · {summary.combine_label}
              {' · '}
              快照 {summary.as_of ?? '—'}
              {summary.first_snapshot
                ? ' · 首次'
                : ` vs 上期 ${summary.previous_as_of ?? '—'} +${summary.added}/-${summary.removed}`}
            </>
          ) : '还没有运行结果'}
          {summary?.hot_hint ? ` · ${summary.hot_hint}` : summary?.hot_updated_at ? ` · 更新于 ${summary.hot_updated_at}` : ''}
        </p>
        {summary?.warnings.map(warning => (
          <p key={warning} className="text-[11px] text-amber-500">{warning}</p>
        ))}
        {sources.isError && <p className="text-xs text-danger">来源列表加载失败</p>}

        {!result && !run.isPending && (
          <p className="py-16 text-center text-sm text-muted">从上面选来源，再点运行。</p>
        )}
        {run.isPending && <p className="py-16 text-center text-sm text-muted">正在计算，重的部分不占页面读数的通道。</p>}
        {result && rows.length === 0 && !run.isPending && (
          <p className="py-16 text-center text-sm text-muted">这次没有符合条件的股票。</p>
        )}

        {rows.length > 0 && (
          <>
            <div className="hidden overflow-x-auto rounded-card border border-border md:block">
              <table className="w-full min-w-[960px] text-left text-xs">
                <thead className="sticky top-0 bg-elevated text-muted">
                  <tr>
                    <th className="px-3 py-2.5">
                      <input
                        type="checkbox"
                        checked={allChecked}
                        onChange={() => setChecked(allChecked ? new Set() : new Set(rows.map(row => row.symbol)))}
                        aria-label="全选"
                      />
                    </th>
                    {['代码', '名称', '行业', '得分', '命中策略', '事件', 'ROE', profitLabel, 'PE', '市值', '变动'].map(title => (
                      <th key={title} className="whitespace-nowrap px-3 py-2.5 font-medium">{title}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map(row => (
                    <tr key={row.symbol} className="border-t border-border hover:bg-elevated/50">
                      <td className="px-3 py-2">
                        <input
                          type="checkbox"
                          checked={checked.has(row.symbol)}
                          onChange={() => setChecked(current => {
                            const next = new Set(current)
                            if (next.has(row.symbol)) next.delete(row.symbol)
                            else next.add(row.symbol)
                            return next
                          })}
                          aria-label={`选择 ${row.symbol}`}
                        />
                      </td>
                      <td className="px-3 py-2">
                        <button type="button" className="font-mono text-accent" onClick={() => setPreview(row.symbol)}>{row.symbol}</button>
                      </td>
                      <td className="px-3 py-2">{row.name || '—'}</td>
                      <td className="px-3 py-2">{row.industry || '—'}</td>
                      <td className="px-3 py-2 tabular-nums">{row.score ?? '—'}</td>
                      <td className="px-3 py-2">
                        <span className="flex flex-wrap gap-0.5">
                          {row.strategies.map(item => <span key={item.id} className={STRATEGY_TAG}>{item.name}</span>)}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-secondary">{row.event || '—'}</td>
                      <td className="px-3 py-2 tabular-nums">{fmtRatioPercent(row.roe)}</td>
                      <td className="px-3 py-2 tabular-nums">{fmtRatioPercent(row.profit_yoy)}</td>
                      <td className="px-3 py-2 tabular-nums">{row.pe == null ? '—' : row.pe.toFixed(1)}</td>
                      <td className="px-3 py-2 tabular-nums">{fmtBigNum(row.market_cap)}</td>
                      <td className="px-3 py-2">{row.change === 'new' ? '新进' : '保留'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="space-y-2 md:hidden">
              {rows.map(row => (
                <article key={row.symbol} className="rounded-card border border-border bg-surface p-3">
                  <div className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={checked.has(row.symbol)}
                      onChange={() => setChecked(current => {
                        const next = new Set(current)
                        if (next.has(row.symbol)) next.delete(row.symbol)
                        else next.add(row.symbol)
                        return next
                      })}
                      aria-label={`选择 ${row.symbol}`}
                    />
                    <div className="min-w-0 flex-1">
                      <button type="button" className="text-left" onClick={() => setPreview(row.symbol)}>
                        <span className="font-mono text-sm text-accent">{row.symbol}</span>
                        <span className="ml-2 text-sm text-foreground">{row.name}</span>
                      </button>
                      <p className="mt-1 text-[11px] text-muted">{row.industry || '—'} · 得分 {row.score ?? '—'} · {row.change === 'new' ? '新进' : '保留'}</p>
                      <div className="mt-1 flex flex-wrap gap-0.5">
                        {row.strategies.map(item => <span key={item.id} className={STRATEGY_TAG}>{item.name}</span>)}
                      </div>
                      {row.event && <p className="mt-1 text-[11px] text-secondary">{row.event}</p>}
                      <p className="mt-1 text-[11px] tabular-nums text-secondary">
                        ROE {fmtRatioPercent(row.roe)} · {profitLabel} {fmtRatioPercent(row.profit_yoy)} · PE {row.pe == null ? '—' : row.pe.toFixed(1)} · {fmtBigNum(row.market_cap)}
                      </p>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          </>
        )}
      </div>
      <StockPreviewDialog
        symbol={preview}
        name={rows.find(row => row.symbol === preview)?.name}
        navList={toNavItems(rows)}
        onNavigate={setPreview}
        onClose={() => setPreview(null)}
      />
    </div>
  )
}

function RangeInput({
  label, min, max, onMin, onMax,
}: {
  label: string
  min: string
  max: string
  onMin: (value: string) => void
  onMax: (value: string) => void
}) {
  const field = 'h-8 w-16 rounded-input border border-border bg-surface px-1.5 text-xs text-foreground'
  return (
    <label className="inline-flex items-center gap-1 text-[11px] text-muted">
      {label}
      <input value={min} onChange={event => onMin(event.target.value)} inputMode="decimal" placeholder="最小" className={field} />
      <span>-</span>
      <input value={max} onChange={event => onMax(event.target.value)} inputMode="decimal" placeholder="最大" className={field} />
    </label>
  )
}

function CheckFilter({
  label, checked, onChange,
}: {
  label: string
  checked: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <label className="inline-flex h-8 items-center gap-1 rounded-btn border border-border bg-surface px-2 text-[11px] text-secondary">
      <input type="checkbox" checked={checked} onChange={event => onChange(event.target.checked)} />
      {label}
    </label>
  )
}
