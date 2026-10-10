import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence } from 'framer-motion'
import {
  Activity,
  ChevronDown,
  Crown,
  Layers3,
  RefreshCw,
  Repeat,
  Search,
  Settings2,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { AnalysisConfigDialog, DimensionHeatmap, PresetFetchState, type AnalysisFieldConfig } from '@/components/analysis-shared'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toNavItems, type NavItem } from '@/lib/listNav'
import { RpsRotationDialog } from '@/components/RpsRotationDialog'
import { api, type MarketSnapshotRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { fmtBigNum, fmtPct, priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'
import {
  groupByIndustryLevel,
  resolveDimension,
  type DimensionGroup,
  type StockRow,
} from '@/lib/analysis-adapter'
import { parseSectorFocus, type SectorKind } from '@/lib/sectorTab'

const PAGE_LIMIT = 12000
const MAX_RENDERED_GROUPS = 120
const MAX_RENDERED_STOCKS = 160

type SortMode = 'heat' | 'avgPct' | 'leader' | 'amount' | 'down'

interface KindSpec {
  kind: SectorKind
  dimLabel: string
  keywords: string[]
  candidateFields: string[]
  presetId: string
  emptyTitle: string
  emptyHint: string
  missingTitle: string
  missingHint: string
  loadingText: string
  unmatchedTitle: string
  unmatchedFallback: string
  heroStrong: string
  heroBreadth: string
  railTitle: string
  searchPlaceholder: string
  leaderStageTitle: string
  gradient: string
  sortActiveClass: string
  railActiveClass: string
  heatBadgeClass: string
  hierarchy: boolean
  heatmap: boolean
}

const KIND_SPEC: Record<SectorKind, KindSpec> = {
  concept: {
    kind: 'concept',
    dimLabel: '概念',
    keywords: ['concept', '概念', 'theme', '题材', '板块'],
    candidateFields: ['concept', '概念', 'theme', '题材', '板块', 'concept_name', '概念名称'],
    presetId: 'ext_gn_ths',
    emptyTitle: '暂无概念数据',
    emptyHint: '从同花顺获取概念分类数据后即可使用概念分析',
    missingTitle: '未获取概念数据',
    missingHint: '内置概念数据源已就绪,点击下方按钮从同花顺获取概念分类数据',
    loadingText: '正在计算概念强度...',
    unmatchedTitle: '未匹配到概念数据',
    unmatchedFallback: '请检查扩展数据是否包含概念/题材相关字段',
    heroStrong: '最强主线',
    heroBreadth: '涨跌板块',
    railTitle: '热度 / 涨跌',
    searchPlaceholder: '搜索概念',
    leaderStageTitle: '本概念三龙头',
    gradient: 'bg-[radial-gradient(circle_at_12%_0%,rgba(59,130,246,0.12),transparent_28%),radial-gradient(circle_at_85%_8%,rgba(244,63,94,0.08),transparent_28%)]',
    sortActiveClass: 'bg-accent/15 text-accent',
    railActiveClass: 'bg-blue-400/[0.08]',
    heatBadgeClass: 'bg-blue-400/10 text-blue-300',
    hierarchy: false,
    heatmap: false,
  },
  industry: {
    kind: 'industry',
    dimLabel: '行业',
    keywords: ['industry', '行业', 'sector', '申万', '中信'],
    candidateFields: ['industry', '行业', 'sector', '申万', '中信', '行业名称', 'industry_name', 'sector_name'],
    presetId: 'ext_hy_ths',
    emptyTitle: '暂无行业数据',
    emptyHint: '从同花顺获取行业分类数据后即可使用行业分析',
    missingTitle: '未获取行业数据',
    missingHint: '内置行业数据源已就绪,点击下方按钮从同花顺获取行业分类数据',
    loadingText: '正在计算行业强度...',
    unmatchedTitle: '未匹配到行业数据',
    unmatchedFallback: '请检查扩展数据是否包含行业/板块相关字段',
    heroStrong: '最强行业',
    heroBreadth: '涨跌行业',
    railTitle: '热度 / 涨跌',
    searchPlaceholder: '搜索行业',
    leaderStageTitle: '本行业三龙头',
    gradient: 'bg-[radial-gradient(circle_at_12%_0%,rgba(245,158,11,0.12),transparent_28%),radial-gradient(circle_at_85%_8%,rgba(244,63,94,0.08),transparent_28%)]',
    sortActiveClass: 'bg-amber-500/15 text-amber-400',
    railActiveClass: 'bg-amber-500/[0.08]',
    heatBadgeClass: 'bg-amber-500/10 text-amber-400',
    hierarchy: true,
    heatmap: true,
  },
}

function configStore(kind: SectorKind) {
  return kind === 'industry' ? storage.industryAnalysisConfig : storage.conceptAnalysisConfig
}

interface EnrichedStock extends MarketSnapshotRow {
  leaderScore: number
  leaderParts: {
    momentum: number
    turnover: number
    amount: number
    cap: number
    volume: number
    boards: number
  }
}

interface SectorStat {
  key: string
  stocks: EnrichedStock[]
  count: number
  avgPct: number | null
  medianPct: number | null
  upCount: number
  downCount: number
  flatCount: number
  upRate: number
  strongCount: number
  weakCount: number
  totalAmount: number
  avgTurnover: number | null
  avgVolRatio: number | null
  leader: EnrichedStock | null
  heatScore: number
  riskScore: number
}

function pickBestConfig(
  configs: { id: string; label: string; description?: string; fields: { name: string; label: string }[] }[],
  keywords: string[],
): string {
  let best = ''
  let bestScore = 0
  for (const c of configs) {
    const haystack = [c.id, c.label, c.description ?? '', ...c.fields.flatMap(f => [f.name, f.label])].join(' ').toLowerCase()
    const score = keywords.reduce((n, k) => n + (haystack.includes(k) ? 1 : 0), 0)
    if (score > bestScore) {
      bestScore = score
      best = c.id
    }
  }
  return best
}

function symbolKeys(symbol: unknown): string[] {
  const raw = String(symbol ?? '').trim()
  if (!raw) return []
  const plain = raw.replace(/\.\w+$/, '')
  return Array.from(new Set([raw, plain]))
}

function buildMarketMap(rows: MarketSnapshotRow[]) {
  const map = new Map<string, MarketSnapshotRow>()
  for (const r of rows) {
    for (const key of symbolKeys(r.symbol)) map.set(key, r)
  }
  return map
}

function clamp01(v: number) {
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(1, v))
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function avg(values: number[]) {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null
}

function median(values: number[]) {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

function leaderScore(stock: MarketSnapshotRow) {
  const pct = num(stock.change_pct) ?? 0
  const turnover = num(stock.turnover_rate) ?? 0
  const amount = num(stock.amount) ?? 0
  const cap = num(stock.float_market_cap) ?? num(stock.market_cap) ?? 0
  const volRatio = num(stock.vol_ratio_5d) ?? 1
  const boards = num(stock.consecutive_limit_ups) ?? 0

  const parts = {
    momentum: clamp01((pct + 0.02) / 0.12),
    turnover: clamp01(Math.log1p(Math.max(turnover, 0)) / Math.log1p(30)),
    amount: clamp01(Math.log1p(Math.max(amount, 0)) / Math.log1p(20_000_000_000)),
    cap: clamp01(Math.log1p(Math.max(cap, 0)) / Math.log1p(300_000_000_000)),
    volume: clamp01((volRatio - 1) / 4),
    boards: clamp01(boards / 5),
  }

  const score = (
    parts.momentum * 0.35 +
    parts.turnover * 0.22 +
    parts.amount * 0.18 +
    parts.cap * 0.15 +
    parts.volume * 0.07 +
    parts.boards * 0.03
  ) * 100

  return { score, parts }
}

function enrichStock(stock: StockRow, marketMap: Map<string, MarketSnapshotRow>): EnrichedStock {
  const market = symbolKeys(stock.symbol ?? stock.code).map(k => marketMap.get(k)).find(Boolean) ?? {}
  const merged = { ...stock, ...market } as MarketSnapshotRow & StockRow
  const ls = leaderScore(merged)
  return {
    ...merged,
    symbol: String(merged.symbol ?? stock.symbol ?? stock.code ?? ''),
    name: merged.name ?? String(stock.name ?? stock['股票简称'] ?? ''),
    leaderScore: ls.score,
    leaderParts: ls.parts,
  }
}

function calcSectorStat(group: DimensionGroup, marketMap: Map<string, MarketSnapshotRow>): SectorStat {
  const seen = new Set<string>()
  const stocks = group.stocks
    .map(s => enrichStock(s, marketMap))
    .filter(s => {
      const key = String(s.symbol ?? '')
      if (!key) return false
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })

  const pctValues = stocks.map(s => num(s.change_pct)).filter((v): v is number => v != null)
  const turnoverValues = stocks.map(s => num(s.turnover_rate)).filter((v): v is number => v != null)
  const volValues = stocks.map(s => num(s.vol_ratio_5d)).filter((v): v is number => v != null)
  const totalAmount = stocks.reduce((sum, s) => sum + (num(s.amount) ?? 0), 0)
  const upCount = pctValues.filter(v => v > 0).length
  const downCount = pctValues.filter(v => v < 0).length
  const flatCount = Math.max(0, stocks.length - upCount - downCount)
  const strongCount = pctValues.filter(v => v >= 0.05).length
  const weakCount = pctValues.filter(v => v <= -0.05).length
  const leader = stocks.length ? [...stocks].sort((a, b) => b.leaderScore - a.leaderScore)[0] : null
  const avgPct = avg(pctValues)
  const medianPct = median(pctValues)
  const upRate = pctValues.length ? upCount / pctValues.length : 0
  const amountScore = clamp01(Math.log1p(totalAmount) / Math.log1p(80_000_000_000))
  const strongScore = stocks.length ? clamp01(strongCount / Math.max(1, stocks.length * 0.18)) : 0
  const leaderPart = clamp01((leader?.leaderScore ?? 0) / 100)
  const avgPart = clamp01(((avgPct ?? 0) + 0.02) / 0.09)
  const upPart = clamp01((upRate - 0.35) / 0.55)

  const heatScore = (avgPart * 0.38 + upPart * 0.2 + strongScore * 0.16 + amountScore * 0.12 + leaderPart * 0.14) * 100
  const riskScore = (clamp01((-(avgPct ?? 0) + 0.01) / 0.08) * 0.55 + clamp01(weakCount / Math.max(1, stocks.length * 0.18)) * 0.45) * 100

  return {
    key: group.key,
    stocks,
    count: stocks.length,
    avgPct,
    medianPct,
    upCount,
    downCount,
    flatCount,
    upRate,
    strongCount,
    weakCount,
    totalAmount,
    avgTurnover: avg(turnoverValues),
    avgVolRatio: avg(volValues),
    leader,
    heatScore,
    riskScore,
  }
}

function statSort(mode: SortMode) {
  return (a: SectorStat, b: SectorStat) => {
    switch (mode) {
      case 'avgPct': return (b.avgPct ?? -Infinity) - (a.avgPct ?? -Infinity)
      case 'leader': return (b.leader?.leaderScore ?? -Infinity) - (a.leader?.leaderScore ?? -Infinity)
      case 'amount': return b.totalAmount - a.totalAmount
      case 'down': return (a.avgPct ?? Infinity) - (b.avgPct ?? Infinity)
      case 'heat':
      default: return b.heatScore - a.heatScore
    }
  }
}

const NARROW_QUERY = '(max-width: 1023px)'

function useNarrow(): boolean {
  return useSyncExternalStore(
    onChange => {
      if (typeof window.matchMedia !== 'function') return () => {}
      const mql = window.matchMedia(NARROW_QUERY)
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    },
    () => typeof window.matchMedia === 'function' && window.matchMedia(NARROW_QUERY).matches,
    () => false,
  )
}

interface SectorSelection {
  kind: SectorKind
  key: string
}

function useSectorBoard(kind: SectorKind) {
  const spec = KIND_SPEC[kind]
  const [fieldConfig, setFieldConfig] = useState<AnalysisFieldConfig>(() => configStore(kind).get({}) as AnalysisFieldConfig)
  const [showConfig, setShowConfig] = useState(false)
  const [search, setSearch] = useState('')
  const [sortMode, setSortMode] = useState<SortMode>('heat')

  const configsQuery = useQuery({ queryKey: QK.extData, queryFn: api.extDataList })
  const availableConfigs = configsQuery.data?.items ?? []
  // 用户配置的 configId 可能已失效 (扩展数据被删除), 此时回退到自动选择。
  const preferredConfigId = fieldConfig.configId || pickBestConfig(availableConfigs, spec.keywords)
  const preferredConfig = availableConfigs.find(c => c.id === preferredConfigId)
  const activeConfigId = preferredConfig ? preferredConfigId : pickBestConfig(availableConfigs, spec.keywords)
  const activeConfig = availableConfigs.find(c => c.id === activeConfigId)

  const rowsQuery = useQuery({
    queryKey: QK.extDataRows(activeConfigId, undefined, PAGE_LIMIT),
    queryFn: () => api.extDataRows(activeConfigId, { limit: PAGE_LIMIT }),
    enabled: !!activeConfigId,
  })

  const queryClient = useQueryClient()
  const fetchMutation = useMutation({
    mutationFn: () => api.extDataPresetFetch(spec.presetId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.extData })
      queryClient.invalidateQueries({ queryKey: QK.extDataRows(spec.presetId, undefined, PAGE_LIMIT) })
    },
  })
  const needsPresetFetch =
    !!activeConfig && activeConfig.id === spec.presetId &&
    !rowsQuery.isLoading && (rowsQuery.data?.total ?? 0) === 0

  const marketQuery = useQuery({
    queryKey: QK.marketSnapshot,
    queryFn: api.marketSnapshot,
    staleTime: 60_000,
  })

  const marketMap = useMemo(() => buildMarketMap(marketQuery.data?.rows ?? []), [marketQuery.data?.rows])
  const resolved = useMemo(
    () => resolveDimension(
      rowsQuery.data,
      activeConfig,
      fieldConfig.dimensionField ? [fieldConfig.dimensionField, ...spec.candidateFields] : spec.candidateFields,
    ),
    [rowsQuery.data, activeConfig, fieldConfig.dimensionField, spec.candidateFields],
  )

  const industryLevel = (fieldConfig.hierarchyLevel ?? 2) as 1 | 2 | 3
  const groups = useMemo(
    () => spec.hierarchy ? groupByIndustryLevel(resolved.groups, industryLevel) : resolved.groups,
    [spec.hierarchy, resolved.groups, industryLevel],
  )

  const stats = useMemo(() => {
    return groups
      .map(g => calcSectorStat(g, marketMap))
      .filter(s => s.count > 0)
  }, [groups, marketMap])

  const filteredStats = useMemo(() => {
    const q = search.trim().toLowerCase()
    const base = q ? stats.filter(s => s.key.toLowerCase().includes(q)) : stats
    return [...base].sort(statSort(sortMode))
  }, [stats, search, sortMode])

  const heatmapQuoteMap = useMemo(() => {
    if (!spec.heatmap) return null
    const map = new Map<string, { symbol: string; pct?: number; change_pct?: number; name?: string; [k: string]: unknown }>()
    for (const [k, v] of marketMap) {
      map.set(k, {
        ...v,
        change_pct: v.change_pct ?? undefined,
        name: v.name ?? undefined,
      })
    }
    return map
  }, [spec.heatmap, marketMap])

  const setIndustryLevel = (level: 1 | 2 | 3) => {
    if (!spec.hierarchy || level === industryLevel) return
    const next = { ...fieldConfig, hierarchyLevel: level }
    setFieldConfig(next)
    configStore(kind).set(next)
  }

  return {
    spec,
    fieldConfig,
    setFieldConfig,
    showConfig,
    setShowConfig,
    search,
    setSearch,
    sortMode,
    setSortMode,
    configsLoading: configsQuery.isLoading,
    activeConfig,
    rowsLoading: rowsQuery.isLoading,
    fetching: rowsQuery.isFetching || marketQuery.isFetching,
    needsPresetFetch,
    fetchPending: fetchMutation.isPending,
    fetchError: fetchMutation.error,
    fetchPreset: () => fetchMutation.mutate(),
    refetch: () => { rowsQuery.refetch(); marketQuery.refetch() },
    groups,
    stats,
    filteredStats,
    heatmapQuoteMap,
    industryLevel,
    setIndustryLevel,
    resolvedHint: resolved.hint || '',
    marketAsOf: marketQuery.data?.as_of ?? null,
  }
}

type SectorBoard = ReturnType<typeof useSectorBoard>

export function SectorAnalysis() {
  const [searchParams] = useSearchParams()
  const focus = parseSectorFocus(searchParams.get('focus') ?? searchParams.get('tab'))
  const narrow = useNarrow()
  const concept = useSectorBoard('concept')
  const industry = useSectorBoard('industry')
  const [open, setOpen] = useState({ concept: true, industry: true })
  const [selected, setSelected] = useState<SectorSelection | null>(null)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState('')
  const [previewNavList, setPreviewNavList] = useState<NavItem[]>([])
  const [matrix, setMatrix] = useState<{ kind: SectorKind; name: string; level?: 1 | 2 | 3 } | null>(null)
  const queryClient = useQueryClient()

  const handleStockClick = useCallback((symbol: string, name?: string, navList?: NavItem[]) => {
    setPreviewSymbol(symbol)
    setPreviewName(name ?? '')
    setPreviewNavList(navList ?? [])
  }, [])

  // 旧地址带 focus 进来：窄屏只展开那一列，并把它滚进视口。
  useEffect(() => {
    if (!focus) return
    const narrowNow = typeof window.matchMedia === 'function' && window.matchMedia(NARROW_QUERY).matches
    if (narrowNow) setOpen({ concept: focus === 'concept', industry: focus === 'industry' })
    document.getElementById(`sector-column-${focus}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [focus])

  const selectSector = (kind: SectorKind, key: string) => {
    setSelected({ kind, key })
    if (narrow) {
      window.setTimeout(() => document.getElementById('sector-detail')?.scrollIntoView?.({ block: 'nearest' }), 0)
    }
  }

  const saveConfig = (kind: SectorKind, config: AnalysisFieldConfig) => {
    const board = kind === 'industry' ? industry : concept
    board.setFieldConfig(config)
    configStore(kind).set(config)
    setSelected(current => (current?.kind === kind ? null : current))
  }

  const asOf = concept.marketAsOf ?? industry.marketAsOf
  const selectedBoard = selected?.kind === 'industry' ? industry : concept
  const selectedStat = selected ? selectedBoard.stats.find(s => s.key === selected.key) ?? null : null

  const header = (
    <PageHeader
      title="板块分析"
      subtitle={asOf ? `概念与行业并排 · ${asOf}` : '概念与行业并排，点击板块查看成分、龙头和 RPS'}
      right={(
        <button
          onClick={() => {
            concept.refetch()
            industry.refetch()
            queryClient.invalidateQueries({ queryKey: ['sector-rotation'] })
            queryClient.invalidateQueries({ queryKey: ['rps-rotation'] })
          }}
          disabled={concept.fetching || industry.fetching}
          className="p-1.5 text-muted hover:bg-surface disabled:opacity-50"
          title="刷新"
        >
          <RefreshCw className={cn('h-4 w-4', (concept.fetching || industry.fetching) && 'animate-spin')} />
        </button>
      )}
    />
  )

  if (concept.configsLoading || industry.configsLoading) {
    return (
      <>
        {header}
        <div className="flex flex-1 items-center justify-center">
          <RefreshCw className="h-5 w-5 animate-spin text-muted" />
        </div>
      </>
    )
  }

  return (
    <>
      {header}
      <div className="min-h-full px-4 py-5 sm:px-6">
        <div className="mx-auto max-w-[1440px] space-y-4">
          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
            <SectorColumn
              board={concept}
              focused={focus === 'concept'}
              collapsed={narrow && !open.concept}
              collapsible={narrow}
              onToggle={() => setOpen(v => ({ ...v, concept: !v.concept }))}
              selectedKey={selected?.kind === 'concept' ? selected.key : null}
              onSelect={key => selectSector('concept', key)}
            />
            <SectorColumn
              board={industry}
              focused={focus === 'industry'}
              collapsed={narrow && !open.industry}
              collapsible={narrow}
              onToggle={() => setOpen(v => ({ ...v, industry: !v.industry }))}
              selectedKey={selected?.kind === 'industry' ? selected.key : null}
              onSelect={key => selectSector('industry', key)}
            />
          </div>
          <SectorDetail
            selection={selected}
            stat={selectedStat}
            level={industry.industryLevel}
            activeSymbol={previewSymbol}
            onStockClick={handleStockClick}
            onOpenMatrix={() => {
              if (!selected) return
              setMatrix({
                kind: selected.kind,
                name: selected.key,
                level: selected.kind === 'industry' ? industry.industryLevel : undefined,
              })
            }}
          />
        </div>
      </div>

      <AnimatePresence>
        {concept.showConfig && (
          <AnalysisConfigDialog
            currentConfig={concept.fieldConfig}
            onSave={config => saveConfig('concept', config)}
            onClose={() => concept.setShowConfig(false)}
          />
        )}
        {industry.showConfig && (
          <AnalysisConfigDialog
            currentConfig={industry.fieldConfig}
            onSave={config => saveConfig('industry', config)}
            onClose={() => industry.setShowConfig(false)}
            showHierarchyLevel
          />
        )}
      </AnimatePresence>
      {previewSymbol && (
        <StockPreviewDialog
          symbol={previewSymbol}
          name={previewName}
          onClose={() => { setPreviewSymbol(null); setPreviewName(''); setPreviewNavList([]) }}
          navList={previewNavList}
          onNavigate={(sym, n) => { setPreviewSymbol(sym); setPreviewName(n ?? '') }}
        />
      )}
      <AnimatePresence>
        {matrix && (
          <RpsRotationDialog
            onClose={() => setMatrix(null)}
            kind={matrix.kind}
            initialSelected={matrix.name}
            initialLevel={matrix.level}
          />
        )}
      </AnimatePresence>
    </>
  )
}

function SectorColumn({
  board,
  focused,
  collapsed,
  collapsible,
  onToggle,
  selectedKey,
  onSelect,
}: {
  board: SectorBoard
  focused: boolean
  collapsed: boolean
  collapsible: boolean
  onToggle: () => void
  selectedKey: string | null
  onSelect: (key: string) => void
}) {
  const { spec } = board
  const priced = board.stats.filter(s => s.avgPct != null)
  const up = priced.filter(s => (s.avgPct ?? 0) > 0).length
  const down = priced.filter(s => (s.avgPct ?? 0) < 0).length
  const strongest = [...board.stats].sort(statSort('heat'))[0]

  return (
    <section
      id={`sector-column-${spec.kind}`}
      data-kind={spec.kind}
      data-focused={focused ? 'true' : 'false'}
      data-collapsed={collapsed ? 'true' : 'false'}
      className={cn(
        'scroll-mt-4 rounded-2xl border bg-surface/70 p-3',
        spec.kind === 'concept' ? 'border-blue-400/25' : 'border-amber-400/30',
        focused && 'ring-2 ring-accent/45',
      )}
    >
      <div className="flex items-start gap-2">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 text-left lg:pointer-events-none"
          aria-expanded={!collapsed}
          aria-controls={`sector-column-body-${spec.kind}`}
          tabIndex={collapsible ? 0 : -1}
          onClick={() => { if (collapsible) onToggle() }}
        >
          <h2 className="text-sm font-semibold text-foreground">{spec.dimLabel}</h2>
          {board.activeConfig && (
            <span className="truncate text-[11px] text-muted">
              {spec.hierarchy ? `${board.industryLevel}级行业 · ` : ''}
              {board.stats.length} 个
              {priced.length > 0 && (
                <>
                  <span className="mx-1 text-muted/40">·</span>
                  <span className="text-bull">{up}</span>
                  <span className="mx-0.5">/</span>
                  <span className="text-bear">{down}</span>
                </>
              )}
            </span>
          )}
          <ChevronDown className={cn('ml-auto h-4 w-4 shrink-0 text-muted transition-transform lg:hidden', !collapsed && 'rotate-180')} />
        </button>
        {spec.hierarchy && (
          <div className="flex items-center rounded-md border border-border bg-base/60 p-0.5" role="group" aria-label="行业层级">
            {([1, 2, 3] as const).map(level => (
              <button
                key={level}
                type="button"
                aria-pressed={board.industryLevel === level}
                onClick={() => board.setIndustryLevel(level)}
                className={cn(
                  'h-6 rounded px-2 text-[10px] font-medium',
                  board.industryLevel === level ? 'bg-accent text-white' : 'text-secondary hover:text-foreground',
                )}
              >
                {level}级
              </button>
            ))}
          </div>
        )}
        <button
          type="button"
          onClick={() => board.setShowConfig(true)}
          className="p-1.5 text-muted hover:bg-elevated hover:text-accent"
          title={`配置${spec.dimLabel}数据源`}
        >
          <Settings2 className="h-4 w-4" />
        </button>
      </div>

      {!collapsed && (
        <div id={`sector-column-body-${spec.kind}`} className="mt-3 space-y-3">
          {!board.activeConfig ? (
            <PresetFetchState
              title={spec.emptyTitle}
              hint={spec.emptyHint}
              isLoading={board.fetchPending}
              error={board.fetchError}
              onFetch={board.fetchPreset}
            />
          ) : (
            <>
              <RotationSummary kind={spec.kind} dimLabel={spec.dimLabel} strongest={strongest} breadthLabel={spec.heroBreadth} strongestLabel={spec.heroStrong} onSelect={onSelect} />
              {board.stats.length > 0 ? (
                <>
                  <SectorRankList
                    spec={spec}
                    stats={board.filteredStats.slice(0, MAX_RENDERED_GROUPS)}
                    total={board.stats.length}
                    selectedKey={selectedKey}
                    search={board.search}
                    sortMode={board.sortMode}
                    onSearch={board.setSearch}
                    onSort={board.setSortMode}
                    onSelect={onSelect}
                  />
                  {spec.heatmap && board.heatmapQuoteMap && board.groups.length > 0 && (
                    <DimensionHeatmap
                      groups={board.groups}
                      quoteMap={board.heatmapQuoteMap}
                      selectedKey={selectedKey}
                      onSelect={key => { if (key) onSelect(key) }}
                      colorScheme="amber"
                    />
                  )}
                </>
              ) : board.rowsLoading ? (
                <div className="rounded-xl border border-border bg-surface px-4 py-10 text-center text-sm text-muted">{spec.loadingText}</div>
              ) : board.needsPresetFetch ? (
                <PresetFetchState
                  title={spec.missingTitle}
                  hint={spec.missingHint}
                  isLoading={board.fetchPending}
                  error={board.fetchError}
                  onFetch={board.fetchPreset}
                />
              ) : (
                <EmptyState icon={Layers3} title={spec.unmatchedTitle} hint={board.resolvedHint || spec.unmatchedFallback} />
              )}
            </>
          )}
        </div>
      )}
    </section>
  )
}

function RotationSummary({
  kind,
  dimLabel,
  strongest,
  strongestLabel,
  breadthLabel,
  onSelect,
}: {
  kind: SectorKind
  dimLabel: string
  strongest?: SectorStat
  strongestLabel: string
  breadthLabel: string
  onSelect: (key: string) => void
}) {
  const query = useQuery({
    queryKey: QK.sectorRotation(kind, undefined, 5, '', 'summary'),
    queryFn: () => api.sectorRotation({ kind, top: 5, autoRows: 5, bucket: 5, sortBy: 'activity' }),
    staleTime: 60_000,
  })
  const latest = query.data?.status === 'ok' ? query.data.timeline.at(-1) : undefined
  const sectors = (query.data?.status === 'ok' ? query.data.sectors : []).slice(0, 5)

  return (
    <div data-rotation={kind} className="rounded-xl border border-border bg-base/40 p-2.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px]">
        <Activity className="h-3.5 w-3.5 text-amber-400" />
        <span className="font-medium text-foreground">{dimLabel}轮动</span>
        {query.isLoading ? (
          <span className="text-muted">正在汇总…</span>
        ) : latest ? (
          <span className="text-muted">
            切换强度 {latest.rotation.toFixed(2)}
            {latest.leader ? ` · 领涨 ${latest.leader}` : ''}
          </span>
        ) : (
          <span className="text-muted">暂无轮动摘要</span>
        )}
      </div>
      {sectors.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {sectors.map(sector => (
            <button
              key={sector.name}
              type="button"
              onClick={() => onSelect(sector.name)}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border/70 bg-surface px-2 py-1 text-[11px] hover:border-accent/40"
            >
              <span className="truncate text-foreground">{sector.name}</span>
              <span className={cn('shrink-0 font-mono tabular-nums', priceColorClass(sector.pct_now))}>
                {sector.pct_now != null ? fmtPct(sector.pct_now) : '—'}
              </span>
            </button>
          ))}
        </div>
      )}
      {strongest && (
        <div className="mt-2 truncate text-[11px] text-muted">
          {strongestLabel} {strongest.key}
          {strongest.avgPct != null && <span className={cn('ml-1 font-mono', priceColorClass(strongest.avgPct))}>{fmtPct(strongest.avgPct)}</span>}
          <span className="mx-1 text-muted/40">·</span>
          {breadthLabel}
        </div>
      )}
    </div>
  )
}

function SectorRankList({
  spec,
  stats,
  total,
  selectedKey,
  search,
  sortMode,
  onSearch,
  onSort,
  onSelect,
}: {
  spec: KindSpec
  stats: SectorStat[]
  total: number
  selectedKey: string | null
  search: string
  sortMode: SortMode
  onSearch: (value: string) => void
  onSort: (value: SortMode) => void
  onSelect: (key: string) => void
}) {
  return (
    <div className="rounded-xl border border-border bg-surface p-2">
      <div className="flex items-center justify-between px-1 pb-2">
        <h3 className="text-xs font-semibold text-foreground">{spec.railTitle}</h3>
        <span className="text-[10px] text-muted">{stats.length}/{total}</span>
      </div>
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
        <input
          value={search}
          onChange={event => onSearch(event.target.value)}
          placeholder={spec.searchPlaceholder}
          className="h-8 w-full rounded-lg border border-border bg-base pl-8 pr-3 text-xs text-foreground outline-none focus:border-accent/50"
        />
      </div>
      <div className="mt-2 grid grid-cols-5 overflow-hidden rounded-lg border border-border text-[10px]">
        {([
          ['heat', '热度'],
          ['avgPct', '涨幅'],
          ['down', '跌幅'],
          ['leader', '龙头'],
          ['amount', '成交'],
        ] as [SortMode, string][]).map(([key, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={sortMode === key}
            onClick={() => onSort(key)}
            className={cn('py-1.5 transition-colors', sortMode === key ? spec.sortActiveClass : 'bg-base text-muted hover:text-foreground')}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="mt-2 max-h-[28rem] overflow-auto rounded-lg border border-border/50">
        {stats.map(item => {
          const active = selectedKey === item.key
          return (
            <button
              key={item.key}
              type="button"
              data-sector={item.key}
              onClick={() => onSelect(item.key)}
              className={cn(
                'w-full border-b border-border/50 px-2.5 py-2 text-left transition-colors last:border-b-0',
                active ? spec.railActiveClass : 'hover:bg-elevated/40',
              )}
            >
              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">{item.key}</span>
                <span className={cn('font-mono text-xs tabular-nums', priceColorClass(item.avgPct))}>{item.avgPct != null ? fmtPct(item.avgPct) : '—'}</span>
              </div>
              <div className="mt-1 flex items-center gap-2 text-[10px] text-muted">
                <span>{item.count}只</span>
                <span className="text-bull">{item.upCount}涨</span>
                <span className="text-bear">{item.downCount}跌</span>
                <span className="ml-auto">强度 {item.heatScore.toFixed(0)}</span>
              </div>
            </button>
          )
        })}
        {stats.length === 0 && <div className="px-3 py-6 text-center text-[11px] text-muted">没有匹配的{spec.dimLabel}</div>}
      </div>
    </div>
  )
}

function SectorDetail({
  selection,
  stat,
  level,
  activeSymbol,
  onStockClick,
  onOpenMatrix,
}: {
  selection: SectorSelection | null
  stat: SectorStat | null
  level: 1 | 2 | 3
  activeSymbol: string | null
  onStockClick: (symbol: string, name?: string, navList?: NavItem[]) => void
  onOpenMatrix: () => void
}) {
  if (!selection) {
    return (
      <section id="sector-detail" className="rounded-2xl border border-dashed border-border px-4 py-8 text-center text-sm text-muted">
        点击上方概念或行业，在这里查看成分股、龙头和 RPS
      </section>
    )
  }

  const spec = KIND_SPEC[selection.kind]
  return (
    <section id="sector-detail" data-detail-kind={selection.kind} className="overflow-hidden rounded-2xl border border-border bg-surface">
      <div className="border-b border-border px-4 py-4 sm:px-5">
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn('rounded-full px-2 py-0.5 text-[10px]', spec.heatBadgeClass)}>{spec.dimLabel}</span>
          <h3 className="text-xl font-semibold text-foreground">{selection.key}</h3>
          {spec.hierarchy && <span className="text-[11px] text-muted">{level}级行业</span>}
          {stat && <span className={cn('rounded-full px-2 py-0.5 text-[10px]', spec.heatBadgeClass)}>强度 {stat.heatScore.toFixed(0)}</span>}
        </div>
        {stat ? (
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
            <span>{stat.count} 只成分</span>
            <span className={priceColorClass(stat.avgPct)}>平均 {stat.avgPct != null ? fmtPct(stat.avgPct) : '—'}</span>
            <span>上涨占比 {(stat.upRate * 100).toFixed(0)}%</span>
            <span>成交额 {fmtBigNum(stat.totalAmount)}</span>
          </div>
        ) : (
          <p className="mt-2 text-xs text-muted">当前列表里没有这个{spec.dimLabel}，下面仍显示它的 RPS 轨迹。</p>
        )}
      </div>

      {stat && <SectorConstituents spec={spec} stat={stat} activeSymbol={activeSymbol} onStockClick={onStockClick} />}
      <SectorRpsStrip kind={selection.kind} name={selection.key} level={spec.hierarchy ? level : undefined} onOpenMatrix={onOpenMatrix} />
    </section>
  )
}

function SectorConstituents({
  spec,
  stat,
  activeSymbol,
  onStockClick,
}: {
  spec: KindSpec
  stat: SectorStat
  activeSymbol: string | null
  onStockClick: (symbol: string, name?: string, navList?: NavItem[]) => void
}) {
  const stocks = [...stat.stocks].sort((a, b) => b.leaderScore - a.leaderScore).slice(0, MAX_RENDERED_STOCKS)
  const topLeaders = stocks.slice(0, 3)
  const focusNav = toNavItems(stocks)
  return (
    <>
      <div className="grid gap-3 border-b border-border bg-base/25 p-4 lg:grid-cols-[1fr_1.15fr]">
        <LeaderStage title={spec.leaderStageTitle} stocks={topLeaders} activeSymbol={activeSymbol} onStockClick={(sym, name) => onStockClick(sym, name, focusNav)} />
        <ScoreExplain stock={topLeaders[0]} />
      </div>
      <div className="max-h-[32rem] overflow-auto">
        <table className="min-w-full text-left text-xs">
          <thead className="bg-elevated/60 text-[11px] text-muted">
            <tr>
              <th className="px-4 py-2 font-medium">排名</th>
              <th className="px-4 py-2 font-medium">股票</th>
              <th className="px-4 py-2 font-medium">涨跌幅</th>
              <th className="px-4 py-2 font-medium">换手率</th>
              <th className="px-4 py-2 font-medium">成交额</th>
              <th className="px-4 py-2 font-medium">流通市值</th>
              <th className="px-4 py-2 font-medium">量比</th>
              <th className="px-4 py-2 font-medium">龙头分</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/70">
            {stocks.map((stock, idx) => (
              <tr
                key={`${stock.symbol}-${idx}`}
                className={cn('cursor-pointer', stock.symbol === activeSymbol ? 'bg-accent/10 hover:bg-accent/15' : 'hover:bg-elevated/30')}
                onClick={() => onStockClick(stock.symbol, stock.name || undefined, focusNav)}
              >
                <td className="px-4 py-2 font-mono text-muted">{idx + 1}</td>
                <td className="px-4 py-2">
                  <div className="font-medium text-foreground">{stock.name || '—'}</div>
                  <div className="font-mono text-[10px] text-muted">{stock.symbol}</div>
                </td>
                <td className={cn('px-4 py-2 font-mono tabular-nums', priceColorClass(stock.change_pct))}>{stock.change_pct != null ? fmtPct(stock.change_pct) : '—'}</td>
                <td className="px-4 py-2 font-mono text-foreground">{stock.turnover_rate != null ? `${stock.turnover_rate.toFixed(2)}%` : '—'}</td>
                <td className="px-4 py-2 font-mono text-foreground">{fmtBigNum(stock.amount)}</td>
                <td className="px-4 py-2 font-mono text-foreground">{fmtBigNum(stock.float_market_cap ?? stock.market_cap)}</td>
                <td className="px-4 py-2 font-mono text-foreground">{stock.vol_ratio_5d != null ? stock.vol_ratio_5d.toFixed(2) : '—'}</td>
                <td className="px-4 py-2">
                  <div className="flex items-center gap-2">
                    <span className="w-9 font-mono text-amber-300">{stock.leaderScore.toFixed(0)}</span>
                    <div className="h-1.5 w-16 rounded-full bg-elevated"><div className="h-full rounded-full bg-amber-300" style={{ width: `${Math.max(4, stock.leaderScore)}%` }} /></div>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {stat.stocks.length > MAX_RENDERED_STOCKS && (
        <div className="border-t border-border px-4 py-2 text-center text-[11px] text-muted">仅展示龙头分前 {MAX_RENDERED_STOCKS} 只，共 {stat.stocks.length} 只</div>
      )}
    </>
  )
}

function shortDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return value
  return `${Number(match[2])}/${match[3]}`
}

function SectorRpsStrip({
  kind,
  name,
  level,
  onOpenMatrix,
}: {
  kind: SectorKind
  name: string
  level?: 1 | 2 | 3
  onOpenMatrix: () => void
}) {
  const days = 12
  const { data, isLoading, isError } = useQuery({
    queryKey: [...QK.rpsRotation(days), kind, level],
    queryFn: () => api.rpsRotation(days, kind, level),
    staleTime: 5 * 60 * 1000,
  })
  const dates = data?.dates ?? []
  const cells = dates.map(date => {
    const column = data?.columns[date] ?? []
    const index = column.findIndex(([sector]) => sector === name)
    return index >= 0 ? { date, rank: index + 1, pct: column[index][1] } : { date, rank: null as number | null, pct: null as number | null }
  })

  return (
    <div className="border-t border-border px-4 py-3 sm:px-5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
          <Repeat className="h-3.5 w-3.5 text-amber-400" />
          涨幅 RPS
          {level ? <span className="text-[10px] font-normal text-muted">{level}级</span> : null}
        </div>
        <button
          type="button"
          onClick={onOpenMatrix}
          className="rounded-md border border-amber-400/40 bg-amber-400/10 px-2 py-1 text-[11px] text-amber-400 hover:bg-amber-400/20"
        >
          完整矩阵
        </button>
      </div>
      {isLoading ? (
        <div className="text-[11px] text-muted">正在读取 RPS…</div>
      ) : isError || cells.length === 0 ? (
        <div className="text-[11px] text-muted">暂无 RPS 数据</div>
      ) : (
        <div className="flex gap-1.5 overflow-x-auto pb-1">
          {cells.map(cell => (
            <div key={cell.date} className="min-w-[4.25rem] rounded-md border border-border/70 bg-base/40 px-1.5 py-1 text-center">
              <div className="text-[10px] text-muted">{shortDate(cell.date)}</div>
              <div className={cn('font-mono text-[11px]', cell.rank != null && cell.rank <= 10 ? 'text-bull' : 'text-secondary')}>
                {cell.rank != null ? `#${cell.rank}` : '—'}
              </div>
              <div className={cn('font-mono text-[10px] tabular-nums', priceColorClass(cell.pct))}>{cell.pct != null ? fmtPct(cell.pct) : '—'}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function LeaderStage({ title, stocks, onStockClick, activeSymbol }: { title: string; stocks: EnrichedStock[]; onStockClick: (symbol: string, name?: string) => void; activeSymbol: string | null }) {
  if (!stocks.length) return <div className="rounded-xl border border-border/60 bg-surface p-4 text-sm text-muted">暂无龙头候选</div>
  return (
    <div className="rounded-xl border border-border/60 bg-surface p-3">
      <div className="mb-2 flex items-center gap-2 text-xs font-medium text-amber-300">
        <Crown className="h-3.5 w-3.5" />
        {title}
      </div>
      <div className="grid gap-2 md:grid-cols-3">
        {stocks.map((stock, idx) => (
          <div key={stock.symbol} onClick={() => onStockClick(stock.symbol, stock.name || undefined)} className={cn('rounded-lg border p-3 cursor-pointer hover:brightness-110 transition-all', idx === 0 ? 'border-amber-400/25 bg-amber-400/[0.06]' : 'border-border/60 bg-base/35', stock.symbol === activeSymbol && 'ring-1 ring-accent/60')}>
            <div className="flex items-center justify-between gap-2">
              <span className={cn('text-[10px] font-medium', idx === 0 ? 'text-amber-300' : 'text-muted')}>{idx === 0 ? '主龙头' : `辅龙 ${idx}`}</span>
              <span className="font-mono text-[11px] text-amber-300">{stock.leaderScore.toFixed(0)}</span>
            </div>
            <div className="mt-2 truncate text-sm font-medium text-foreground">{stock.name || stock.symbol}</div>
            <div className="mt-0.5 flex items-center justify-between text-[11px]">
              <span className="font-mono text-muted">{stock.symbol}</span>
              <span className={cn('font-mono', priceColorClass(stock.change_pct))}>{stock.change_pct != null ? fmtPct(stock.change_pct) : '—'}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function ScoreExplain({ stock }: { stock?: EnrichedStock }) {
  if (!stock) return <div className="rounded-xl border border-border/60 bg-surface p-4 text-sm text-muted">暂无评分拆解</div>
  const parts = stock.leaderParts
  return (
    <div className="rounded-xl border border-border/60 bg-surface p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-foreground">主龙头评分拆解</span>
        <span className="text-[11px] text-muted">涨幅 / 换手 / 成交 / 市值 / 量比 / 连板</span>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
        <Part label="动能" value={parts.momentum} cls="bg-rose-400" />
        <Part label="换手" value={parts.turnover} cls="bg-orange-400" />
        <Part label="成交" value={parts.amount} cls="bg-blue-400" />
        <Part label="市值" value={parts.cap} cls="bg-cyan-400" />
        <Part label="量比" value={parts.volume} cls="bg-purple-400" />
        <Part label="连板" value={parts.boards} cls="bg-amber-300" />
      </div>
    </div>
  )
}

function Part({ label, value, cls }: { label: string; value: number; cls: string }) {
  return <div className="rounded-lg bg-base/35 px-2 py-1.5"><div className="mb-1 flex justify-between text-[10px] text-muted"><span>{label}</span><span>{Math.round(value * 100)}</span></div><div className="h-1 rounded-full bg-elevated"><div className={cn('h-full rounded-full', cls)} style={{ width: `${Math.max(3, value * 100)}%` }} /></div></div>
}
