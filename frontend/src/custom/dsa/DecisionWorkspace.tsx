import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import {
  Bell,
  Bot,
  CalendarClock,
  Compass,
  FileText,
  Gauge,
  Images,
  LineChart,
  MessagesSquare,
  Newspaper,
  PieChart,
  RefreshCw,
  ScanSearch,
  Settings2,
  ChevronDown,
  ShieldAlert,
  Sparkles,
  Upload,
} from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/PageHeader'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import {
  asRecords,
  sampleRecords,
  dsaCommand,
  dsaEtfRotation,
  type EtfRotationHolding,
  type EtfRotationView,
  dsaUpstream,
  fetchDsaCatalog,
  fetchDsaQuantEvidence,
  fetchDsaLedgerAudit,
  type DsaLedgerAudit,
  type DsaQuantEvidence,
  fetchDsaSchedule,
  fetchDsaStatus,
  fetchShareImage,
  isSniperRegisterSuccess,
  registerSniperLines,
  saveDsaSchedule,
  isRecord,
  textOf,
  type DsaError,
  type SniperLineInput,
} from './client'
import { DashboardPanel } from './DashboardPanel'
import { DsaEnablementChecklist } from './DsaEnablementChecklist'
import { formatPercentPoints } from './dashboardHistory'
import {
  buildFunnelDiagnostics,
  bumpCoreAction,
  DSA_FUNNEL_L4_ACTION_NOTES,
  DSA_FUNNEL_L4_ACTIONS,
  DSA_FUNNEL_L4_STATUS,
  DSA_FUNNEL_PENDING_NOTES,
  incrementOpenWorkspace,
  readActionCount,
  recordStatusSample,
} from './funnelMetrics'
import { PortfolioRiskView } from './portfolioRisk'
import {
  SAMPLE_ALERTS,
  SAMPLE_NEWS,
  SAMPLE_REPORT,
  SAMPLE_RISK,
  SAMPLE_SCREEN,
} from './samples'

const SECTIONS = [
  { id: 'dashboard', label: '决策仪表盘', icon: Gauge },
  { id: 'reports', label: '个股研报', icon: FileText },
  { id: 'intelligence', label: '情报', icon: Newspaper },
  { id: 'markets', label: '多市场', icon: Compass },
  { id: 'screening', label: '多市场选股', icon: ScanSearch },
  { id: 'etf', label: 'ETF 轮动', icon: LineChart },
  { id: 'chat', label: '问股', icon: MessagesSquare },
  { id: 'bot', label: '机器人', icon: Bot },
  { id: 'schedule', label: '定时推送', icon: CalendarClock },
  { id: 'alerts', label: '决策预警', icon: Bell },
  { id: 'signals', label: '决策信号', icon: Sparkles },
  { id: 'portfolio', label: '持仓风险', icon: PieChart },
  { id: 'backtest', label: '决策回测', icon: ShieldAlert },
  { id: 'import', label: '智能导入', icon: Upload },
  { id: 'settings', label: '用量配置', icon: Settings2 },
] as const

type SectionId = (typeof SECTIONS)[number]['id']

const primaryBtn = 'inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs text-white shadow-sm shadow-accent/25 hover:bg-accent/90 disabled:opacity-50'
/**
 * 上游回测 / 纸面战绩未过 TSP 审计时的固定徽章（P-ledger A7）。
 *
 * 这个字符串是**契约**：后端 `ledger_audit.TSP_AUDIT_BADGE` 是同一句，
 * `gdsaledgerIsolation.test.ts` 会钉住两边一致。审计状态未翻转前，
 * 任何展示上游结算数字的地方都要带它。
 */
export const TSP_AUDIT_BADGE = '未经 TSP 审计'

const ghostBtn = 'inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-3 text-xs text-secondary hover:text-foreground disabled:opacity-50'
const fieldCls = 'h-8 rounded-btn border border-border bg-base px-2.5 text-xs text-foreground outline-none focus:border-accent'

function Panel({ title, hint, children, extra }: { title: string; hint?: string; children: ReactNode; extra?: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-surface">
      <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div>
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
          {hint && <p className="mt-0.5 text-[11px] leading-relaxed text-muted">{hint}</p>}
        </div>
        {extra}
      </header>
      <div className="px-4 py-3">{children}</div>
    </section>
  )
}

function Notice({ children }: { children: ReactNode }) {
  return <p className="rounded-btn bg-elevated px-3 py-2 text-xs leading-relaxed text-secondary">{children}</p>
}

function RawJson({ data }: { data: unknown }) {
  if (data == null) return null
  return (
    <details data-raw-json className="mt-3">
      <summary className="cursor-pointer text-xs text-secondary">原始数据</summary>
      <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-secondary">{JSON.stringify(data, null, 2)}</pre>
    </details>
  )
}

function countOf(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '—'
}

function formatWan(value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  if (abs < 10_000) return `${sign}${Math.round(abs).toLocaleString('zh-CN')}`
  const unit = abs >= 100_000_000 ? '亿' : '万'
  const scaled = abs / (unit === '亿' ? 100_000_000 : 10_000)
  return `${sign}${scaled.toFixed(2).replace(/\.?0+$/, '')}${unit}`
}

function parsedJson(text: string): unknown | null {
  const trimmed = text.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null
  try {
    return JSON.parse(trimmed) as unknown
  } catch {
    return null
  }
}

const CAPABILITY_STATUS: Record<string, string> = {
  ok: '可用',
  partial: '部分可用',
  unconfigured: '未配置',
  unavailable: '不可用',
  unknown: '未知',
  degraded: '降级',
  stale: '过期',
}

export function CapabilitySummary({ data }: { data: unknown }) {
  if (!isRecord(data)) return null
  const providers = asRecords(data.providers)
  const datasets = asRecords(data.datasets)
  const priorities = asRecords(data.priorities)
  if (providers.length === 0 && datasets.length === 0 && priorities.length === 0) {
    return <p className="text-xs text-muted">还没有数据源能力说明。</p>
  }
  return (
    <div className="space-y-3">
      {providers.length > 0 && (
        <ul className="space-y-2">
          {providers.map(item => (
            <li key={textOf(item.name)} className="rounded-lg border border-border bg-base px-3 py-2">
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-sm text-foreground">{textOf(item.label, textOf(item.name))}</span>
                <span className="shrink-0 text-[11px] text-secondary">{CAPABILITY_STATUS[textOf(item.status, '')] ?? textOf(item.status)}</span>
              </div>
              <p className="mt-0.5 text-[11px] leading-relaxed text-muted">
                {Array.isArray(item.markets) ? item.markets.join('、') : '—'}
                {Array.isArray(item.datasets) && item.datasets.length > 0 ? ` · ${item.datasets.join('、')}` : ''}
              </p>
              {typeof item.last_error === 'string' && item.last_error && <p className="mt-1 text-[11px] text-warning">{item.last_error}</p>}
            </li>
          ))}
        </ul>
      )}
      {datasets.length > 0 && (
        <ul className="divide-y divide-border text-xs">
          {datasets.map(item => (
            <li key={textOf(item.dataset)} className="flex items-baseline justify-between gap-3 py-1.5">
              <span className="min-w-0 truncate text-foreground">{textOf(item.dataset)}</span>
              <span className="shrink-0 text-muted">{CAPABILITY_STATUS[textOf(item.status, '')] ?? textOf(item.status)}{item.source ? ` · ${textOf(item.source, '')}` : ''}</span>
            </li>
          ))}
        </ul>
      )}
      {priorities.length > 0 && (
        <ul className="space-y-1 text-[11px] text-secondary">
          {priorities.map(item => (
            <li key={textOf(item.scenario)}>
              {textOf(item.scenario)}：{Array.isArray(item.providers) ? item.providers.join(' → ') : '—'}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function BacktestSummary({ data }: { data: unknown }) {
  if (!isRecord(data)) return null
  const stats = [
    ['候选', data.processed],
    ['写入', data.saved],
    ['完成', data.completed],
    ['数据不足', data.insufficient],
    ['错误', data.errors],
  ] as const
  return (
    <div className="mt-3 space-y-2">
      <dl data-backtest-summary className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {stats.map(([label, value]) => (
          <div key={label} className="rounded-lg border border-border bg-base px-3 py-2">
            <dt className="text-[11px] text-muted">{label}</dt>
            <dd className="num text-sm text-foreground">{countOf(value)}</dd>
          </div>
        ))}
      </dl>
      <p className="text-[11px] text-muted">评估窗口 {countOf(data.applied_eval_window_days)} 个交易日</p>
      {typeof data.message === 'string' && data.message && <p className="text-xs leading-relaxed text-secondary">{data.message}</p>}
    </div>
  )
}

export function ImportSummary({ data }: { data: unknown }) {
  if (!isRecord(data)) return null
  const items = asRecords(data.items)
  const codes = Array.isArray(data.codes) ? data.codes.filter((item): item is string => typeof item === 'string' && item.length > 0) : []
  if (items.length === 0 && codes.length === 0) {
    return <EmptyState icon={Upload} title="没有识别出代码" hint="换一段文本或文件再试。" />
  }
  const rows: Record<string, unknown>[] = items.length > 0 ? items : codes.map(code => ({ code }))
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full min-w-[24rem] text-left text-xs">
        <thead className="text-muted">
          <tr>
            <th className="py-1 pr-3 font-normal">代码</th>
            <th className="py-1 pr-3 font-normal">名称</th>
            <th className="py-1 font-normal">置信度</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${textOf(row.code, 'row')}-${index}`} className="border-t border-border">
              <td className="whitespace-nowrap py-1.5 pr-3 font-mono text-foreground">{textOf(row.code)}</td>
              <td className="whitespace-nowrap py-1.5 pr-3">{textOf(row.name, '')}</td>
              <td className="whitespace-nowrap py-1.5 text-secondary">{textOf(row.confidence, '')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const USAGE_CALL_TYPES: Record<string, string> = {
  analysis: '个股分析',
  agent: '问股',
  market_review: '大盘复盘',
}

export function UsageSummary({ data }: { data: unknown }) {
  if (!isRecord(data)) return null
  const period = textOf(data.period, '')
  const periodLabel = period === 'today' ? '今日' : period === 'month' ? '本月' : period === 'all' ? '全部' : period || '—'
  const byType = asRecords(data.by_call_type)
  return (
    <div className="mt-2 space-y-3">
      <p className="text-[11px] text-muted">{textOf(data.from_date, '')} 至 {textOf(data.to_date, '')}</p>
      <dl data-usage-summary className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {([
          ['周期', periodLabel],
          ['调用', countOf(data.total_calls)],
          ['输入 token', formatWan(data.total_prompt_tokens)],
          ['输出 token', formatWan(data.total_completion_tokens)],
          ['合计 token', formatWan(data.total_tokens)],
        ] as [string, string][]).map(([label, value]) => (
          <div key={label} className="rounded-lg border border-border bg-base px-3 py-2">
            <dt className="text-[11px] text-muted">{label}</dt>
            <dd className="num text-sm text-foreground">{value}</dd>
          </div>
        ))}
      </dl>
      {byType.length > 0 && (
        <div data-usage-types className="overflow-x-auto">
          <table className="w-full min-w-[28rem] text-left text-xs">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-3 font-normal">类型</th>
                <th className="py-1 pr-3 font-normal">次数</th>
                <th className="py-1 pr-3 font-normal">输入</th>
                <th className="py-1 pr-3 font-normal">输出</th>
                <th className="py-1 font-normal">合计</th>
              </tr>
            </thead>
            <tbody>
              {byType.map(row => (
                <tr key={textOf(row.call_type)} className="border-t border-border">
                  <td className="whitespace-nowrap py-1.5 pr-3 text-foreground">{USAGE_CALL_TYPES[textOf(row.call_type, '')] ?? textOf(row.call_type)}</td>
                  <td className="num whitespace-nowrap py-1.5 pr-3">{countOf(row.calls)}</td>
                  <td className="num whitespace-nowrap py-1.5 pr-3">{formatWan(row.prompt_tokens)}</td>
                  <td className="num whitespace-nowrap py-1.5 pr-3">{formatWan(row.completion_tokens)}</td>
                  <td className="num whitespace-nowrap py-1.5">{formatWan(row.total_tokens)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Failure({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : '加载失败'
  return (
    <div className="flex items-center justify-between gap-3 rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-secondary">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className={ghostBtn} onClick={onRetry}>
          <RefreshCw className="h-3.5 w-3.5" />
          重试
        </button>
      )}
    </div>
  )
}

export function DecisionWorkspace() {
  const [params, setParams] = useSearchParams()
  const requested = params.get('section')
  const section: SectionId = SECTIONS.some(item => item.id === requested)
    ? requested as SectionId
    : 'dashboard'
  const code = params.get('code') ?? ''
  const status = useQuery({ queryKey: QK.dsaStatus, queryFn: fetchDsaStatus, refetchInterval: 30_000, retry: false })
  const catalog = useQuery({ queryKey: QK.dsaCatalog, queryFn: fetchDsaCatalog, staleTime: 60_000, retry: false })
  const reachable = status.data?.reachable === true
  const healthDetail = status.data?.detail?.trim() ?? ''
  const healthLabel = status.isLoading
    ? '检测中'
    : reachable
      ? '服务已连接'
      : healthDetail && healthDetail.length <= 28
        ? `未连接 · ${healthDetail}`
        : '服务未连接'
  const [showSample, setShowSample] = useState(true)
  const sample = !reachable && showSample

  // 漏斗 L3：打开决策页计一次。用 ref 守住「每个组件实例只 +1」，
  // 避免 StrictMode 双调用 / 重渲染把打开次数刷高。
  const openedRef = useRef(false)
  useEffect(() => {
    if (openedRef.current) return
    openedRef.current = true
    incrementOpenWorkspace()
  }, [])

  // 漏斗 L1/L2：只跟随既有 30s status 查询结果写入。去重由 recordStatusSample
  // 内部按 dataUpdatedAt 完成（同一份 fetch 结果只计一次），这里不另开紧循环。
  useEffect(() => {
    if (!status.data) return
    recordStatusSample(status.data, status.dataUpdatedAt)
  }, [status.data, status.dataUpdatedAt])

  const select = (id: SectionId) => {
    const next = new URLSearchParams(params)
    next.set('section', id)
    setParams(next, { replace: true })
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-base">
      <PageHeader
        title="决策"
        helpId="dsa"
        subtitle="多市场 AI 研报、情报、定时推送和问股。行情与策略回测仍走原来的页面。"
        titleExtra={
          <span
            title={status.data?.detail || undefined}
            className={cn(
              'rounded-full px-2 py-0.5 text-[10px]',
              reachable ? 'bg-bear/15 text-bear' : 'bg-warning/15 text-warning',
            )}
          >
            {healthLabel}
          </span>
        }
        right={!reachable ? (
          <button type="button" className={ghostBtn} onClick={() => setShowSample(value => !value)}>
            {sample ? '隐藏样例' : '查看样例'}
          </button>
        ) : undefined}
      />
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <nav className="flex shrink-0 gap-1 overflow-x-auto border-b border-border px-3 py-2 md:w-40 md:flex-col md:overflow-y-auto md:border-b-0 md:border-r">
          {SECTIONS.map(item => {
            const Icon = item.icon
            const active = item.id === section
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => select(item.id)}
                className={cn(
                  'flex shrink-0 items-center gap-2 rounded-btn px-2.5 py-2 text-left text-xs transition-colors',
                  active ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated hover:text-foreground',
                )}
              >
                <Icon className="h-3.5 w-3.5 shrink-0" />
                {item.label}
              </button>
            )
          })}
        </nav>
        <div className="min-h-0 min-w-0 flex-1 overflow-auto px-4 pb-52 pt-4 md:px-6 md:py-4">
          {sample && (
            <div className="mb-3">
              <Notice>下面带「样例」标记的内容只用于查看版式，不是行情，也不能当作交易依据。连接决策服务并配置模型密钥后会换成真实结果。</Notice>
            </div>
          )}
          {section === 'dashboard' && (
            <DashboardPanel
              sample={sample}
              reachable={reachable}
              footer={<QuantEvidence />}
              review={<MarketReview reachable={reachable} />}
            />
          )}
          {section === 'reports' && <Reports sample={sample} reachable={reachable} initialCode={code} />}
          {section === 'intelligence' && <Intelligence sample={sample} reachable={reachable} />}
          {section === 'markets' && <Markets reachable={reachable} markets={catalog.data?.markets ?? []} />}
          {section === 'screening' && <Screening sample={sample} reachable={reachable} />}
          {section === 'etf' && <EtfRotation />}
          {section === 'chat' && <Chat reachable={reachable} />}
          {section === 'bot' && <BotConsole commands={catalog.data?.bot_commands ?? []} />}
          {section === 'schedule' && <Schedule reachable={reachable} />}
          {section === 'alerts' && <Alerts sample={sample} reachable={reachable} />}
          {section === 'signals' && <Signals reachable={reachable} initialCode={code} />}
          {section === 'portfolio' && <Portfolio sample={sample} reachable={reachable} />}
          {section === 'backtest' && <DecisionBacktest reachable={reachable} initialCode={code} />}
          {section === 'import' && <ImportCodes reachable={reachable} />}
          {section === 'settings' && (
            <SettingsPanel
              features={catalog.data?.features ?? []}
              envVars={catalog.data?.env_vars ?? []}
              commit={catalog.data?.upstream_commit}
              reachable={reachable}
              detail={status.data?.detail}
            />
          )}
          <p className="mt-4 text-[10px] text-muted">研究结果仅供学习，不构成投资建议。</p>
        </div>
      </div>
    </div>
  )
}

export function QuantEvidencePanel({
  data,
  defaultOpen = false,
}: {
  data: DsaQuantEvidence
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const scope = [
    data.meta.period,
    data.meta.universe != null ? `全市场 ${data.meta.universe} 只` : '',
    data.meta.benchmark_return ? `同期基准 ${data.meta.benchmark_return}` : '',
  ].filter(Boolean).join('，')
  const hint = scope
    ? `${scope}。同名技能的提示里会附上这些机械回测结果。`
    : '同名技能的提示里会附上这些机械回测结果。'
  return (
    <section className="rounded-lg border border-border bg-surface">
      <button
        type="button"
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        <span className="min-w-0">
          <span className="block text-sm font-medium text-foreground">量化回测参考</span>
          {open && <span className="mt-0.5 block text-[11px] leading-relaxed text-muted">{hint}</span>}
        </span>
        <ChevronDown className={cn('h-4 w-4 shrink-0 text-muted transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="border-t border-border px-4 py-3">
          <Notice>{data.caveat}</Notice>
          {data.expired && (
            <p data-quant-expired className="mt-2 text-[11px] leading-relaxed text-secondary">
              证据已过期（as_of {data.meta.freshness?.as_of || '—'}，{data.meta.freshness?.reason || '过期'}），不再注入技能提示。
            </p>
          )}
          {!data.expired && data.sample_below_threshold && data.skills.length > 0 && (
            <p data-quant-sample-below className="mt-2 text-[11px] leading-relaxed text-secondary">
              样本量不足门槛（{data.min_sample_for_guidance ?? 100}），这些数字只作方向参考，不注入技能提示。
            </p>
          )}
          <ul className="mt-3 space-y-2">
            {data.skills.map(skill => (
              <li key={skill.name} className="text-xs leading-relaxed text-secondary">
                <span className="font-mono text-foreground">{skill.name}</span>
                {skill.matched.length > 0 && <span className="text-muted"> · {skill.matched.join('、')}</span>}
                {skill.returns.length > 0 && (
                  <span className="ml-2">
                    {skill.returns.map((value, index) => (
                      <span key={`${skill.name}-${index}`}>
                        {index > 0 && <span className="text-muted"> / </span>}
                        <span className={cn('num', priceColorClass(value))}>{formatPercentPoints(value)}</span>
                      </span>
                    ))}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

export function LedgerAuditPanel({ data }: { data: DsaLedgerAudit }) {
  const summary = data.summary
  const badge = summary.badge || TSP_AUDIT_BADGE
  if (!data.available) {
    return (
      <Panel title="证据账本审计" hint="读上游 backtest_results（只读）。取不到账本时不显示任何比率。">
        <Notice>
          {badge} · {summary.unavailable_reason || '账本暂不可读'}
        </Notice>
      </Panel>
    )
  }
  return (
    <Panel
      title="证据账本审计"
      hint="上游 backtest_results 的只读视图。分母已排除不可成交与中性样本；样本不足时不给百分比。"
      extra={<span data-tsp-audit-badge className="rounded-btn bg-elevated px-2 py-1 text-[11px] text-secondary">{badge}</span>}
    >
      <dl data-ledger-summary className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          ['样本', `${summary.rows_total}`],
          ['计入分母', `${summary.denominator}`],
          ['不可成交', `${summary.unfillable_count}`],
          ['排除', `${summary.excluded_from_denominator}`],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg border border-border bg-base px-3 py-2">
            <dt className="text-[11px] text-muted">{label}</dt>
            <dd className="num text-sm text-foreground">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-[11px] leading-relaxed text-muted">
        {summary.win_rate_pct == null
          ? `样本不足（门槛 ${summary.min_sample_for_percent}），不对外给胜率百分比。`
          : `胜率 ${summary.win_rate_pct.toFixed(2)}%。`}
        红为赢、绿为负（红涨绿跌）。
      </p>
      <p className="mt-1 text-[11px] leading-relaxed text-muted">
        入场口径：{summary.entry_basis.join('；') || '—'}
      </p>
      {data.rows.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[30rem] text-left text-xs">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-3 font-normal">代码</th>
                <th className="py-1 pr-3 font-normal">结果</th>
                <th className="py-1 pr-3 font-normal">收盘入场</th>
                <th className="py-1 pr-3 font-normal">次日开盘</th>
                <th className="py-1 font-normal">分母</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.slice(0, 20).map(row => (
                <tr key={`${row.code}-${row.analysis_history_id}`} className="border-t border-border">
                  <td className="py-1 pr-3 font-mono text-foreground">{row.code}</td>
                  <td className={cn('py-1 pr-3', priceColorClass(row.outcome === 'win' ? 1 : row.outcome === 'loss' ? -1 : 0))}>
                    {row.outcome || '—'}
                  </td>
                  <td className="num py-1 pr-3 text-secondary">{row.entry_close ?? '—'}</td>
                  <td className="num py-1 pr-3 text-secondary">{row.entry_next_open ?? '未注入'}</td>
                  <td className="py-1 text-muted">{row.in_denominator ? '计入' : row.unfillable ? '不可成交' : '不计入'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.audit_items && data.audit_items.length > 0 && (
        <ul data-ledger-audit-items className="mt-3 space-y-1">
          {data.audit_items.map(item => (
            <li key={item.id} className="text-[11px] leading-relaxed text-muted">
              <span className="font-mono text-foreground">{item.id}</span> {item.title} · {item.state}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

function LedgerAudit() {
  const query = useQuery({
    queryKey: ['dsaLedgerAudit'],
    queryFn: fetchDsaLedgerAudit,
    staleTime: 300_000,
    retry: false,
  })
  if (!query.data) return null
  return <LedgerAuditPanel data={query.data} />
}

function QuantEvidence() {
  const query = useQuery({
    queryKey: QK.dsaQuantEvidence,
    queryFn: fetchDsaQuantEvidence,
    staleTime: 300_000,
    retry: false,
  })
  const data = query.data
  if (!data?.available) return null
  return <QuantEvidencePanel data={data} />
}

function MarketReview({ reachable }: { reachable: boolean }) {
  const [region, setRegion] = useState('cn')
  const mutation = useMutation({
    mutationFn: () => dsaUpstream('analysis/market-review', {
      method: 'POST',
      body: JSON.stringify({ send_notification: false, region }),
    }),
  })
  return (
    <form className="flex items-center gap-1.5" onSubmit={event => { event.preventDefault(); mutation.mutate() }}>
      <select className={fieldCls} value={region} onChange={event => setRegion(event.target.value)} aria-label="复盘市场">
        <option value="cn">A 股</option>
        <option value="hk">港股</option>
        <option value="us">美股</option>
        <option value="cn,hk,us">中港美</option>
      </select>
      <button className={primaryBtn} type="submit" disabled={!reachable || mutation.isPending}>
        {mutation.isPending ? '复盘中' : '大盘复盘'}
      </button>
    </form>
  )
}

export function Reports({ sample, reachable, initialCode }: { sample: boolean; reachable: boolean; initialCode: string }) {
  const [code, setCode] = useState(initialCode || '600519')
  const [recordId, setRecordId] = useState<string>(sample ? '1' : '')
  const qc = useQueryClient()
  const analyze = useMutation({
    mutationFn: () => dsaUpstream('analysis/analyze', {
      method: 'POST',
      body: JSON.stringify({
        stock_code: code.trim(),
        report_type: 'detailed',
        async_mode: true,
        notify: false,
        original_query: code.trim(),
        selection_source: 'manual',
      }),
    }),
    // L4 只认**成功**：mutation 的 onSuccess 才自增，失败/被 disabled 都不算留存动作
    onSuccess: () => {
      bumpCoreAction('report_submit')
      qc.invalidateQueries({ queryKey: QK.dsaUpstream('history') })
    },
  })
  const report = useQuery({
    queryKey: QK.dsaUpstream(`markdown:${recordId}`),
    queryFn: () => dsaUpstream<{ content?: string }>(`history/${encodeURIComponent(recordId)}/markdown`),
    enabled: reachable && recordId !== '' && recordId !== '1',
    retry: false,
  })
  const markdown = sample && recordId === '1'
    ? SAMPLE_REPORT
    : (report.data?.content ?? '')

  return (
    <div className="space-y-3">
      <Panel title="提交分析" hint="异步生成，完成后在历史里打开。页面触发默认不推送，避免误发到群。">
        <form className="flex flex-wrap items-center gap-2" onSubmit={event => { event.preventDefault(); analyze.mutate() }}>
          <input className={cn(fieldCls, 'w-40 font-mono')} value={code} onChange={event => setCode(event.target.value)} placeholder="600519 / hk00700 / AAPL" aria-label="股票代码" />
          <button className={primaryBtn} type="submit" disabled={!reachable || !code.trim() || analyze.isPending}>生成研报</button>
          <label className="text-[11px] text-muted">
            记录编号
            <input className={cn(fieldCls, 'ml-1.5 w-24 font-mono')} value={recordId} onChange={event => setRecordId(event.target.value)} aria-label="记录编号" />
          </label>
        </form>
        {recordId && recordId !== '1' && <ShareImage recordId={recordId} />}
        {analyze.isError && <div className="mt-2"><Failure error={analyze.error} /></div>}
        {analyze.isSuccess && <p className="mt-2 text-xs text-secondary">已提交。任务状态可在调度页查看，完成后用记录编号打开全文。</p>}
      </Panel>
      <Panel title={sample && recordId === '1' ? '报告全文 · 样例' : '报告全文'}>
        {report.isError && <Failure error={report.error} onRetry={() => report.refetch()} />}
        {markdown ? <MarkdownRenderer content={markdown} /> : <Notice>输入记录编号后加载 Markdown。样例模式下编号 1 展示版式。</Notice>}
      </Panel>
      <SniperRegisterPanel reachable={reachable} defaultSymbol={code} analysisId={recordId} />
    </div>
  )
}

/**
 * 「登记狙击线」—— 把三价（理想买入 / 止损 / 目标）写进 DSA 的价位线。
 *
 * 仅供研究，不构成投资建议；执行须人工确认。
 *
 * ## 这是价位线，不是监控规则引擎（软提醒 3）
 *
 * 它存的是**三个价格**，到价提醒。和「决策预警（alerts / rules）」那套规则引擎
 * 完全是两回事 —— 后者能配条件、周期、组合逻辑。文案里两个词必须分开写，
 * 混用会让人以为「登记了三价」等于「建了监控规则」。
 *
 * ## 什么时候按钮该灰
 *
 * `reachable !== true` 就灰。`enabled` 恒真，拿它门控等于没门控。
 *
 * ## 失败 / 「没生效」都要照实说
 *
 * 后端在**没有有效价位**时返回 `replaced: false` 并**保留旧线**（不是报错）。
 * 这是预期行为，但 UI 必须明说「没生效，旧线还在」，绝不能渲染成成功。
 * 三价全空时直接在前端拦住 —— 这种请求发出去注定 `replaced: false`，白跑一趟。
 */
function SniperRegisterPanel({
  reachable,
  defaultSymbol,
  analysisId,
}: {
  reachable: boolean
  defaultSymbol: string
  analysisId: string
}) {
  const [symbol, setSymbol] = useState(defaultSymbol)
  const [idealBuy, setIdealBuy] = useState('')
  const [stopLoss, setStopLoss] = useState('')
  const [takeProfit, setTakeProfit] = useState('')
  const [message, setMessage] = useState<{ tone: 'ok' | 'warn' | 'err'; text: string } | null>(null)

  // 报告区的代码改了，登记区跟着走（用户不必手抄一遍代码）
  useEffect(() => {
    if (defaultSymbol) setSymbol(current => (current === '' ? defaultSymbol : current))
  }, [defaultSymbol])

  const mutation = useMutation({
    mutationFn: (body: SniperLineInput) => registerSniperLines(body),
    onSuccess: result => {
      // L4 判据只在 client 里定义一处：replaced===true 或 lines.length>0
      if (isSniperRegisterSuccess(result)) {
        bumpCoreAction('sniper_register')
        setMessage({ tone: 'ok', text: `已登记 ${result.symbol} 的价位线（${result.lines.length} 条）。这是价位线，不是监控规则。` })
      } else {
        setMessage({
          tone: 'warn',
          text: '没生效：没有解析出有效价位，服务端保留了原有价位线。请至少填一个价格后重试。',
        })
      }
    },
    onError: (error: Error) => setMessage({ tone: 'err', text: error.message }),
  })

  /** 空串 → null；非数字 → NaN（交给 parseFloat 后的 isFinite 判）。 */
  const toPrice = (raw: string): number | null => {
    const trimmed = raw.trim()
    if (!trimmed) return null
    const value = Number.parseFloat(trimmed)
    return Number.isFinite(value) ? value : Number.NaN
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setMessage(null)
    const target = symbol.trim()
    if (!target) {
      setMessage({ tone: 'err', text: '请填股票代码。' })
      return
    }
    const body: SniperLineInput = {
      symbol: target,
      ideal_buy: toPrice(idealBuy),
      stop_loss: toPrice(stopLoss),
      take_profit: toPrice(takeProfit),
    }
    if ([body.ideal_buy, body.stop_loss, body.take_profit].some(value => value !== null && Number.isNaN(value))) {
      setMessage({ tone: 'err', text: '价格填的不是数字。' })
      return
    }
    // 三价全空 → 后端必然 replaced=false，别发这一趟
    if (body.ideal_buy == null && body.stop_loss == null && body.take_profit == null) {
      setMessage({ tone: 'warn', text: '至少填一个价格（理想买入 / 止损 / 目标）才能登记。' })
      return
    }
    if (analysisId && analysisId !== '1') body.analysis_id = analysisId
    mutation.mutate(body)
  }

  return (
    <Panel
      title="登记狙击线"
      hint="价位线：存理想买入 / 止损 / 目标三个价格，到价提醒。与「决策预警」的监控规则不是一回事。"
    >
      <form className="flex flex-wrap items-center gap-2" onSubmit={submit}>
        <input
          className={cn(fieldCls, 'w-32 font-mono')}
          value={symbol}
          onChange={event => setSymbol(event.target.value)}
          placeholder="600519"
          aria-label="狙击线股票代码"
        />
        <input
          className={cn(fieldCls, 'w-24 font-mono')}
          value={idealBuy}
          onChange={event => setIdealBuy(event.target.value)}
          placeholder="理想买入"
          aria-label="理想买入价"
          inputMode="decimal"
        />
        <input
          className={cn(fieldCls, 'w-24 font-mono')}
          value={stopLoss}
          onChange={event => setStopLoss(event.target.value)}
          placeholder="止损"
          aria-label="止损价"
          inputMode="decimal"
        />
        <input
          className={cn(fieldCls, 'w-24 font-mono')}
          value={takeProfit}
          onChange={event => setTakeProfit(event.target.value)}
          placeholder="目标"
          aria-label="目标价"
          inputMode="decimal"
        />
        <button
          className={primaryBtn}
          type="submit"
          disabled={!reachable || mutation.isPending}
          data-dsa-sniper-register
        >
          {mutation.isPending ? '登记中' : '登记狙击线'}
        </button>
        {!reachable && (
          <span className="text-[11px] text-muted" data-dsa-sniper-gated>
            决策服务未连接，无法登记
          </span>
        )}
      </form>
      <p className="mt-2 text-[11px] leading-relaxed text-muted">
        价位从报告里自己填；本拍未做自动解析（自动解析属 design_pending）。
        返回「未生效」时旧价位线仍在，不会把已有线索抹掉。
      </p>
      {message && (
        <p
          className={cn(
            'mt-2 text-xs leading-relaxed',
            message.tone === 'ok' ? 'text-bear' : message.tone === 'warn' ? 'text-warning' : 'text-bull',
          )}
          data-dsa-sniper-message={message.tone}
        >
          {message.text}
        </p>
      )}
    </Panel>
  )
}

function ShareImage({ recordId }: { recordId: string }) {
  const [preview, setPreview] = useState('')
  const [message, setMessage] = useState('')
  const previewRef = useRef('')
  const replacePreview = (url: string) => {
    if (previewRef.current) URL.revokeObjectURL(previewRef.current)
    previewRef.current = url
    setPreview(url)
  }
  useEffect(() => () => {
    if (previewRef.current) URL.revokeObjectURL(previewRef.current)
  }, [])
  const load = useMutation({
    mutationFn: () => fetchShareImage(recordId),
    onSuccess: url => {
      setMessage('')
      replacePreview(url)
    },
    onError: (error: Error) => {
      replacePreview('')
      setMessage(error.message || '分享图暂时无法生成。请确认已安装 wkhtmltopdf 和中文字体。')
    },
  })
  return (
    <div className="mt-2 space-y-2">
      <button className={ghostBtn} type="button" disabled={load.isPending} onClick={() => load.mutate()}>
        <Images className="h-3.5 w-3.5" />
        {load.isPending ? '生成中' : '分享图'}
      </button>
      {message && <p className="max-w-xl text-[11px] leading-relaxed text-warning">{message}</p>}
      {preview && (
        <a href={preview} download={`dsa-report-${recordId}.png`} className="block max-w-sm">
          <img src={preview} alt="报告分享图" className="rounded-lg border border-border" />
        </a>
      )}
    </div>
  )
}

function Intelligence({ sample, reachable }: { sample: boolean; reachable: boolean }) {
  const query = useQuery({
    queryKey: QK.dsaUpstream('intelligence'),
    queryFn: () => dsaUpstream<unknown>('intelligence/items?limit=20'),
    enabled: reachable,
    retry: false,
  })
  const fetchAll = useMutation({
    mutationFn: () => dsaUpstream('intelligence/sources/fetch-enabled', { method: 'POST' }),
  })
  const rows = reachable ? asRecords(query.data) : sample ? sampleRecords(SAMPLE_NEWS) : []
  return (
    <Panel
      title={sample ? '情报 · 样例' : '情报'}
      hint="拉取已启用的情报源。搜索密钥未配置时，上游会返回明确失败而不是空新闻冒充结果。"
      extra={<button className={primaryBtn} type="button" disabled={!reachable || fetchAll.isPending} onClick={() => fetchAll.mutate()}>立即拉取</button>}
    >
      {query.isError && <Failure error={query.error} onRetry={() => query.refetch()} />}
      {fetchAll.isError && <div className="mb-2"><Failure error={fetchAll.error} /></div>}
      {rows.length === 0 && !query.isError && (
        <EmptyState
          icon={Newspaper}
          title={reachable ? '情报库是空的' : '决策服务未连接'}
          hint={reachable ? '先在上游配置搜索源，再点立即拉取。' : undefined}
        />
      )}
      <ul className="mt-2 divide-y divide-border">
        {rows.map((row, index) => (
          <li key={`${textOf(row.title, 'item')}-${index}`} className="py-2">
            <div className="text-sm text-foreground">{textOf(row.title, textOf(row.summary))}</div>
            <div className="mt-0.5 text-[11px] text-muted">{textOf(row.source, textOf(row.source_name, ''))} {textOf(row.published_at, textOf(row.created_at, ''))}</div>
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function Markets({ reachable, markets }: { reachable: boolean; markets: { id: string; label: string; examples: string }[] }) {
  const query = useQuery({
    queryKey: QK.dsaUpstream('capabilities'),
    queryFn: () => dsaUpstream<unknown>('data/capabilities'),
    enabled: reachable,
    retry: false,
  })
  return (
    <div className="space-y-3">
      <Panel title="代码口径" hint="这些市场由 DSA 的数据源路由处理。TSP 本地库仍服务 A 股和已开启的 ETF。">
        <div className="grid gap-2 sm:grid-cols-2">
          {markets.map(item => (
            <div key={item.id} className="rounded-lg border border-border bg-base px-3 py-2">
              <div className="text-sm text-foreground">{item.label}</div>
              <div className="mt-0.5 font-mono text-[11px] text-muted">{item.examples}</div>
            </div>
          ))}
        </div>
      </Panel>
      <Panel title="上游数据能力">
        {!reachable && <Notice>连接决策服务后，这里显示各数据源实际可用的能力。</Notice>}
        {query.isError && <Failure error={query.error} onRetry={() => query.refetch()} />}
        {query.data != null && (
          <>
            <CapabilitySummary data={query.data} />
            <RawJson data={query.data} />
          </>
        )}
      </Panel>
    </div>
  )
}

function Screening({ sample, reachable }: { sample: boolean; reachable: boolean }) {
  const strategies = useQuery({
    queryKey: QK.dsaUpstream('screen-strategies'),
    queryFn: () => dsaUpstream<unknown>('screening/strategies'),
    enabled: reachable,
    retry: false,
  })
  const names = asRecords(strategies.data).map(item => textOf(item.id, textOf(item.name, ''))).filter(Boolean)
  const [strategy, setStrategy] = useState('')
  const [market, setMarket] = useState('cn')
  const run = useMutation({
    mutationFn: () => dsaUpstream('screening/screen', {
      method: 'POST',
      body: JSON.stringify({ strategy: strategy || names[0], market, max_results: 20 }),
    }),
  })
  const liveRows = asRecords(run.data)
  const rows = liveRows.length ? liveRows : sample && !run.data ? sampleRecords(SAMPLE_SCREEN) : []
  return (
    <Panel title={sample && !run.data ? '选股结果 · 样例' : '多市场选股'} hint="这是 DSA 的规则选股，和策略页的全 A 向量扫描分开。">
      <form className="mb-3 flex flex-wrap gap-2" onSubmit={event => { event.preventDefault(); run.mutate() }}>
        <input className={cn(fieldCls, 'w-44')} value={strategy} placeholder={names[0] || '策略 id'} onChange={event => setStrategy(event.target.value)} aria-label="选股策略" />
        <select className={fieldCls} value={market} onChange={event => setMarket(event.target.value)} aria-label="市场">
          <option value="cn">A 股</option>
          <option value="hk">港股</option>
          <option value="us">美股</option>
        </select>
        <button className={primaryBtn} type="submit" disabled={!reachable || run.isPending}>开始筛选</button>
      </form>
      {strategies.isError && <Failure error={strategies.error} />}
      {run.isError && <div className="mb-2"><Failure error={run.error} /></div>}
      {rows.length === 0 ? (
        <EmptyState icon={ScanSearch} title="还没有筛选结果" hint="选择策略和市场后开始筛选。" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] text-left text-xs">
            <thead className="text-muted">
              <tr><th className="py-1 pr-3 font-normal">代码</th><th className="pr-3 font-normal">名称</th><th className="pr-3 font-normal">市场</th><th className="pr-3 font-normal">分数</th><th className="font-normal">说明</th></tr>
            </thead>
            <tbody>
              {rows.map(row => (
                <tr key={textOf(row.code, textOf(row.stock_code))} className="border-t border-border">
                  <td className="whitespace-nowrap py-1.5 pr-3 font-mono">{textOf(row.code, textOf(row.stock_code))}</td>
                  <td className="whitespace-nowrap pr-3">{textOf(row.name, textOf(row.stock_name))}</td>
                  <td className="whitespace-nowrap pr-3 uppercase">{textOf(row.market, '')}</td>
                  <td className="num whitespace-nowrap pr-3">{textOf(row.score, '')}</td>
                  <td className="min-w-[12rem] text-secondary">{textOf(row.reason, textOf(row.summary, ''))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function formatEtfScore(kind: string | undefined, score: number | null) {
  if (score == null || !Number.isFinite(score)) return '—'
  if (kind === 'momentum') return `${(score * 100).toFixed(1)}%`
  if (kind === 'none') return '—'
  return score.toFixed(2)
}

export function EtfRotationCard({ result }: { result: EtfRotationView }) {
  const kind = result.score_kind
  const scoreLabel = kind === 'momentum' ? '动量' : kind === 'none' ? '得分' : '平均排名'
  const showBucket = result.holdings.some(row => row.bucket)
  const basis = result.position_basis === 'target'
    ? '今日为调仓日，下表是目标持仓，下一交易日收盘执行。'
    : '下表是当前规则持仓。'
  return (
    <div className="mt-3 overflow-hidden rounded-lg border border-border bg-base">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-3 py-2.5">
        <div>
          <div className="text-sm text-foreground">{result.mode_label || result.mode}</div>
          <div className="mt-0.5 text-[11px] text-muted">信号日 {result.signal_date}</div>
        </div>
        <div className="text-right text-[11px] leading-5 text-secondary">
          <div>上次调仓 {result.last_rebalance || '—'}</div>
          <div>下次调仓 {result.next_rebalance || '—'}</div>
        </div>
      </div>
      {result.holdings.length === 0 ? (
        <EmptyState icon={PieChart} title="这一轮没有持仓" hint="规则没有选出标的。" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[28rem] text-left text-xs">
            <thead className="text-muted">
              <tr>
                <th className="px-3 py-1.5 font-normal">标的</th>
                {showBucket && <th className="py-1.5 pr-3 font-normal">分桶</th>}
                <th className="py-1.5 pr-3 font-normal">权重</th>
                <th className="px-3 py-1.5 font-normal">{scoreLabel}</th>
              </tr>
            </thead>
            <tbody>
              {result.holdings.map(row => (
                <EtfHoldingRow key={`${row.code}-${row.bucket}`} row={row} kind={kind} showBucket={showBucket} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="space-y-1 px-3 py-2 text-[11px] leading-relaxed text-muted">
        <p>{basis}</p>
        {result.risk_off && <p>组合回撤已触发防守仓位。</p>}
        {result.score_hint && <p>{result.score_hint}</p>}
        <p>{result.disclaimer}</p>
      </div>
    </div>
  )
}

function EtfHoldingRow({ row, kind, showBucket }: { row: EtfRotationHolding; kind: string | undefined; showBucket: boolean }) {
  const title = row.name && row.name !== row.code ? `${row.name} ${row.code}` : row.code
  return (
    <tr className="border-t border-border">
      <td className="px-3 py-1.5 text-foreground">{title}</td>
      {showBucket && <td className="py-1.5 text-secondary">{row.bucket || '—'}</td>}
      <td className="num py-1.5">{(row.weight * 100).toFixed(1)}%</td>
      <td className="num px-3 py-1.5">{formatEtfScore(kind, row.score)}</td>
    </tr>
  )
}

export function EtfRotationPanel({
  pending,
  error,
  detail,
  result,
  onRun,
}: {
  pending: boolean
  error: unknown
  detail: string
  result: EtfRotationView | null
  onRun: () => void
}) {
  return (
    <Panel title="ETF 轮动" hint="不调用大模型。默认分桶混合动量，每月调仓。Docker 下在 dsa 服务里执行。">
      <button className={primaryBtn} type="button" disabled={pending} onClick={onRun}>
        {pending ? '正在计算…' : '运行轮动'}
      </button>
      {error != null && <div className="mt-2"><Failure error={error} /></div>}
      {!result && !detail && !pending && (
        <p className="mt-3 text-xs leading-relaxed text-muted">
          信号在收盘计算，下一交易日收盘执行。规则结果不代表未来收益，不构成投资建议。没有 sidecar 时会说明原因。
        </p>
      )}
      {result && <EtfRotationCard result={result} />}
      {!result && detail && <div className="mt-3"><Notice>{detail}</Notice></div>}
      {result && detail && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs text-secondary">运行日志</summary>
          <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-secondary">{detail}</pre>
        </details>
      )}
    </Panel>
  )
}

function asEtfView(value: unknown): EtfRotationView | null {
  if (!isRecord(value) || !Array.isArray(value.holdings) || typeof value.signal_date !== 'string') return null
  const holdings = value.holdings.filter(isRecord).map(row => ({
    code: textOf(row.code, ''),
    name: textOf(row.name, textOf(row.code, '')),
    bucket: textOf(row.bucket, ''),
    weight: typeof row.weight === 'number' ? row.weight : Number(row.weight) || 0,
    score: typeof row.score === 'number' ? row.score : null,
  }))
  return {
    signal_date: value.signal_date,
    mode: textOf(value.mode, ''),
    mode_label: textOf(value.mode_label, textOf(value.mode, '')),
    holdings,
    last_rebalance: typeof value.last_rebalance === 'string' ? value.last_rebalance : null,
    next_rebalance: typeof value.next_rebalance === 'string' ? value.next_rebalance : null,
    disclaimer: textOf(value.disclaimer, '规则结果不代表未来收益，不构成投资建议。'),
    score_kind: textOf(value.score_kind, 'avg_rank'),
    score_hint: textOf(value.score_hint, ''),
    position_basis: textOf(value.position_basis, 'current'),
    risk_off: value.risk_off === true,
  }
}

function EtfRotation() {
  const run = useMutation({ mutationFn: dsaEtfRotation })
  const detail = isRecord(run.data) ? textOf(run.data.detail, '') : ''
  const result = isRecord(run.data) && run.data.ok !== false ? asEtfView(run.data.result) : null
  return (
    <EtfRotationPanel
      pending={run.isPending}
      error={run.isError ? run.error : null}
      detail={detail}
      result={result}
      onRun={() => run.mutate()}
    />
  )
}

export function Chat({ reachable }: { reachable: boolean }) {
  const [skill, setSkill] = useState('')
  const [input, setInput] = useState('')
  const [lines, setLines] = useState<{ role: 'user' | 'assistant'; text: string; raw?: unknown }[]>([])
  const send = useMutation({
    mutationFn: (message: string) => dsaUpstream<Record<string, unknown>>('agent/chat', {
      method: 'POST',
      body: JSON.stringify({ message, skills: skill.trim() ? [skill.trim()] : undefined }),
    }),
    onSuccess: data => {
      // L4 只认成功：拿到回复才算「留存动作」；onError 分支不自增
      bumpCoreAction('chat_send')
      const text = ['reply', 'content', 'message', 'text'].map(key => data[key]).find(item => typeof item === 'string') as string | undefined
      setLines(current => [...current, { role: 'assistant', text: text || '没有可显示的回复。', raw: text ? undefined : data }])
    },
    onError: (error: DsaError) => {
      setLines(current => [...current, { role: 'assistant', text: error.message }])
    },
  })
  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    const message = input.trim()
    if (!message) return
    setLines(current => [...current, { role: 'user', text: message }])
    setInput('')
    send.mutate(message)
  }
  return (
    <Panel title="策略问股" hint="面板里的 AI 助手仍负责查因子和跑回测。这里只和 DSA Agent 对话。">
      {!reachable && <Notice>服务未连接时无法提问。可以先到「机器人」看命令说明。</Notice>}
      <div className="mt-2 space-y-2">
        {lines.map((line, index) => (
          <div key={`${line.role}-${index}`} className={cn('rounded-lg px-3 py-2 text-xs leading-relaxed', line.role === 'user' ? 'bg-accent/10 text-foreground' : 'bg-elevated text-secondary')}>
            {line.text}
            {line.raw != null && <RawJson data={line.raw} />}
          </div>
        ))}
      </div>
      <form className="mt-3 flex flex-wrap gap-2" onSubmit={onSubmit}>
        <input className={cn(fieldCls, 'w-36')} value={skill} placeholder="技能，可空" onChange={event => setSkill(event.target.value)} aria-label="策略技能" />
        <input className={cn(fieldCls, 'min-w-0 flex-1')} value={input} placeholder="例如：用趋势策略看 600519" onChange={event => setInput(event.target.value)} aria-label="问题" />
        <button className={primaryBtn} type="submit" disabled={!reachable || send.isPending}>发送</button>
      </form>
    </Panel>
  )
}

function BotConsole({ commands }: { commands: { name: string; usage: string; summary: string }[] }) {
  const [input, setInput] = useState('/help')
  const [lines, setLines] = useState<{ title: string; text: string }[]>([])
  const send = useMutation({
    mutationFn: dsaCommand,
    onSuccess: data => setLines(current => [...current, { title: data.title, text: data.text }]),
    onError: (error: Error) => setLines(current => [...current, { title: '未能执行', text: error.message }]),
  })
  return (
    <div className="space-y-3">
      <Panel title="命令" hint="和飞书、钉钉、Telegram、Discord、Slack 里的斜杠命令同一套。/help 不依赖上游。">
        <ul className="grid gap-1 sm:grid-cols-2">
          {commands.map(item => (
            <li key={item.name}>
              <button type="button" className="w-full rounded-btn px-2 py-1.5 text-left hover:bg-elevated" onClick={() => setInput(item.usage.split(' ')[0])}>
                <span className="font-mono text-xs text-foreground">{item.usage}</span>
                <span className="mt-0.5 block text-[11px] text-muted">{item.summary}</span>
              </button>
            </li>
          ))}
        </ul>
        <form className="mt-3 flex gap-2" onSubmit={event => { event.preventDefault(); send.mutate(input) }}>
          <input className={cn(fieldCls, 'flex-1 font-mono')} value={input} onChange={event => setInput(event.target.value)} aria-label="机器人命令" />
          <button className={primaryBtn} type="submit" disabled={send.isPending || !input.trim()}>执行</button>
        </form>
        <div className="mt-3 space-y-2">
          {lines.map((line, index) => {
            const json = parsedJson(line.text)
            if (json != null) {
              return (
                <div key={`${line.title}-${index}`} className="rounded-lg bg-base px-3 py-2">
                  <p className="text-xs text-foreground">{line.title}</p>
                  <RawJson data={json} />
                </div>
              )
            }
            return (
              <pre key={`${line.title}-${index}`} className="overflow-auto whitespace-pre-wrap rounded-lg bg-base px-3 py-2 font-mono text-[11px] leading-relaxed text-secondary">
                {line.title}{'\n'}{line.text}
              </pre>
            )
          })}
        </div>
      </Panel>
    </div>
  )
}

const NOTIFICATION_TEST_CHANNELS = [
  { value: 'dingtalk', label: '钉钉' },
  { value: 'feishu', label: '飞书' },
  { value: 'wechat', label: '企业微信' },
  { value: 'telegram', label: 'Telegram' },
  { value: 'discord', label: 'Discord' },
  { value: 'slack', label: 'Slack' },
  { value: 'email', label: '邮件' },
] as const

export function Schedule({ reachable }: { reachable: boolean }) {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: QK.dsaSchedule,
    queryFn: fetchDsaSchedule,
    enabled: reachable,
    retry: false,
  })
  const [enabled, setEnabled] = useState(false)
  const [time, setTime] = useState('18:00')
  const [tradingDaysOnly, setTradingDaysOnly] = useState(true)
  const [region, setRegion] = useState('cn')
  const [watchlist, setWatchlist] = useState('')
  useEffect(() => {
    const data = query.data
    if (!data) return
    setEnabled(data.enabled)
    setTime(data.time || '18:00')
    setTradingDaysOnly(data.trading_days_only)
    setRegion(data.region || 'cn')
    setWatchlist(data.watchlist)
  }, [query.data])
  const save = useMutation({
    mutationFn: () => saveDsaSchedule({
      enabled,
      time,
      trading_days_only: tradingDaysOnly,
      region,
      watchlist,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.dsaSchedule })
      qc.invalidateQueries({ queryKey: QK.dsaUpstream('history') })
    },
  })
  const run = useMutation({
    mutationFn: () => dsaUpstream('system/scheduler/run-now', { method: 'POST' }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.dsaSchedule }) },
  })
  const [channel, setChannel] = useState<string>(NOTIFICATION_TEST_CHANNELS[0].value)
  const test = useMutation({
    mutationFn: () => dsaUpstream('system/config/notification/test-channel', {
      method: 'POST',
      body: JSON.stringify({ channel }),
    }),
  })
  const scheduler = query.data?.scheduler
  const extraTimes = query.data?.extra_times ?? []
  return (
    <div className="space-y-3">
      <Panel title="定时任务" hint="时钟在决策服务进程里，时区 Asia/Shanghai。只在交易日分析下面的自选，结果进入决策仪表盘，并按已配置的通知渠道推送。TSP 的盘后管道不受这里影响。" extra={
        <button className={primaryBtn} type="button" disabled={!reachable || run.isPending} onClick={() => run.mutate()}>立即跑一轮</button>
      }>
        {!reachable && <Notice>服务未连接。可先在 .env 写好 SCHEDULE_ENABLED、SCHEDULE_TIME 和 STOCK_LIST，再用 docker compose --profile dsa up --build 启动。</Notice>}
        {query.isError && <Failure error={query.error} onRetry={() => query.refetch()} />}
        <form className="mt-3 space-y-3" onSubmit={event => { event.preventDefault(); save.mutate() }}>
          <label className="flex items-center gap-2 text-xs text-secondary">
            <input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />
            启用每日定时分析
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-[11px] text-muted">
              上海时间
              <input className={cn(fieldCls, 'ml-1.5 w-28 font-mono')} value={time} onChange={event => setTime(event.target.value)} placeholder="18:00" aria-label="定时时刻" inputMode="numeric" maxLength={5} required />
            </label>
            <label className="text-[11px] text-muted">
              复盘市场
              <select className={cn(fieldCls, 'ml-1.5')} value={region} onChange={event => setRegion(event.target.value)} aria-label="复盘市场">
                <option value="cn">A 股</option>
                <option value="hk">港股</option>
                <option value="us">美股</option>
                <option value="cn,hk,us">中港美</option>
              </select>
            </label>
            <label className="flex items-center gap-2 text-xs text-secondary">
              <input type="checkbox" checked={tradingDaysOnly} onChange={event => setTradingDaysOnly(event.target.checked)} />
              仅交易日
            </label>
          </div>
          <label className="block text-[11px] text-muted">
            自选列表
            <textarea
              className="mt-1 min-h-16 w-full rounded-btn border border-border bg-base px-2.5 py-2 font-mono text-xs text-foreground outline-none focus:border-accent"
              value={watchlist}
              onChange={event => setWatchlist(event.target.value)}
              placeholder="600519,000858"
              aria-label="自选列表"
            />
          </label>
          <button className={primaryBtn} type="submit" disabled={!reachable || save.isPending}>保存定时设置</button>
        </form>
        {extraTimes.length > 1 && (
          <p className="mt-2 text-[11px] leading-relaxed text-secondary">当前文件里还有多个时点：{extraTimes.join('、')}。保存后只保留上面这一个。</p>
        )}
        {scheduler && (
          <dl className="mt-3 grid gap-1 text-[11px] text-secondary sm:grid-cols-2">
            <div>调度{scheduler.enabled ? '已启动' : '未启动'}{scheduler.running ? '，本轮进行中' : ''}</div>
            <div>下次 {scheduler.next_run_at ? scheduler.next_run_at.replace('T', ' ').slice(0, 16) : '—'}</div>
            <div>上次成功 {scheduler.last_success_at ? scheduler.last_success_at.replace('T', ' ').slice(0, 16) : '—'}</div>
            <div>最近跳过 {textOf(scheduler.last_skip_reason, '—')}</div>
          </dl>
        )}
        {scheduler?.last_error && <p className="mt-2 text-[11px] leading-relaxed text-warning">{scheduler.last_error}</p>}
        {save.isError && <div className="mt-2"><Failure error={save.error} /></div>}
        {save.isSuccess && <p className="mt-2 text-xs text-secondary">已写入 .env，并让决策服务重新加载定时任务。启动服务本身不会立刻分析。</p>}
        {run.isError && <div className="mt-2"><Failure error={run.error} /></div>}
        {run.isSuccess && <p className="mt-2 text-xs text-secondary">已提交本轮。完成后到决策仪表盘查看，通知发往已配置的渠道。</p>}
      </Panel>
      <Panel title="通知渠道试发" hint="渠道密钥写在 .env。这里只发一条测试，不改 TSP 监控推送。">
        <form className="flex gap-2" onSubmit={event => { event.preventDefault(); test.mutate() }}>
          <select className={fieldCls} value={channel} onChange={event => setChannel(event.target.value)} aria-label="通知渠道">
            {NOTIFICATION_TEST_CHANNELS.map(item => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
          <button className={ghostBtn} type="submit" disabled={!reachable || test.isPending}>发送测试</button>
        </form>
        {test.isError && <div className="mt-2"><Failure error={test.error} /></div>}
        {test.isSuccess && <p className="mt-2 text-xs text-secondary">已提交测试。若密钥为空，上游会返回失败原因。</p>}
      </Panel>
    </div>
  )
}

function Alerts({ sample, reachable }: { sample: boolean; reachable: boolean }) {
  const query = useQuery({
    queryKey: QK.dsaUpstream('alert-rules'),
    queryFn: () => dsaUpstream<unknown>('alerts/rules'),
    enabled: reachable,
    retry: false,
  })
  const rows = reachable ? asRecords(query.data) : sample ? sampleRecords(SAMPLE_ALERTS) : []
  return (
    <Panel title={sample ? '决策预警 · 样例' : '决策预警'} hint="这些规则来自分析结论。监控中心的价格和信号规则不会出现在这里。">
      {query.isError && <Failure error={query.error} onRetry={() => query.refetch()} />}
      {rows.length === 0 && !query.isError && <EmptyState icon={Bell} title="还没有决策预警" />}
      <ul className="divide-y divide-border">
        {rows.map((row, index) => {
          const enabled = typeof row.enabled === 'boolean' ? (row.enabled ? '启用' : '停用') : textOf(row.status, '')
          const target = textOf(row.target, textOf(row.symbol, textOf(row.stock_code, '')))
          const kind = textOf(row.alert_type, '')
          const severity = textOf(row.severity, '')
          return (
            <li key={`${textOf(row.id, textOf(row.name, 'rule'))}-${index}`} className="py-2">
              <div className="flex items-baseline justify-between gap-3">
                <span className="min-w-0 truncate text-sm text-foreground">{textOf(row.name, textOf(row.title))}</span>
                {enabled && <span className="shrink-0 text-[11px] text-secondary">{enabled}</span>}
              </div>
              <p className="mt-0.5 text-[11px] leading-relaxed text-muted">
                {[target, kind, severity].filter(Boolean).join(' · ') || '—'}
              </p>
              <p className="text-[11px] text-muted">最近触发 {textOf(row.last_triggered_at, textOf(row.last, '尚未触发'))}</p>
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}

const SIGNAL_ACTIONS: Record<string, string> = {
  buy: '买入',
  add: '加仓',
  hold: '持有',
  reduce: '减仓',
  sell: '卖出',
  watch: '观望',
  avoid: '回避',
  alert: '预警',
}
const SIGNAL_HORIZONS: Record<string, string> = {
  intraday: '日内',
  '1d': '1 日',
  '3d': '3 日',
  '5d': '5 日',
  '10d': '10 日',
  swing: '波段',
  long: '长期',
}
const SIGNAL_STATUSES: Record<string, string> = {
  active: '生效中',
  expired: '已过期',
  invalidated: '已失效',
  closed: '已结束',
  archived: '已归档',
}
const SIGNAL_OUTCOMES: Record<string, string> = {
  hit: '命中',
  miss: '未中',
  neutral: '中性',
}

export interface DecisionSignalRow {
  id: string
  direction: string
  directionTone: string
  horizon: string
  status: string
  createdAt: string
  outcome: string
  outcomeTone: string
}

function signalLabel(map: Record<string, string>, value: unknown, fallback = '—') {
  const text = textOf(value, '')
  if (!text) return fallback
  return map[text] ?? text
}

function signalDirectionTone(action: string) {
  if (action === 'buy' || action === 'add') return 'text-bull'
  if (action === 'sell' || action === 'reduce' || action === 'avoid') return 'text-bear'
  if (action === 'hold' || action === 'watch' || action === 'alert') return 'text-warning'
  return 'text-secondary'
}

function signalOutcomeTone(outcome: string) {
  if (outcome === 'hit') return 'text-bull'
  if (outcome === 'miss') return 'text-bear'
  if (outcome === 'unable') return 'text-warning'
  return 'text-muted'
}

function signalTime(value: unknown) {
  const text = textOf(value, '')
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/)
  return match ? `${match[1]} ${match[2]}` : (text || '—')
}

function outcomePagesBySignal(pages: readonly unknown[]) {
  const grouped = new Map<string, Record<string, unknown>[]>()
  for (const page of pages) {
    for (const row of asRecords(page)) {
      const id = textOf(row.signal_id, '')
      if (!id) continue
      const bucket = grouped.get(id) ?? []
      bucket.push(row)
      grouped.set(id, bucket)
    }
  }
  return grouped
}

function outcomeSummary(rows: Record<string, unknown>[], horizon: string) {
  if (rows.length === 0) return { text: '尚未复盘', tone: 'text-muted' }
  const matched = rows.filter(row => textOf(row.horizon, '') === horizon)
  const chosen = matched.length > 0 ? matched : rows
  const describe = (row: Record<string, unknown>) => {
    const outcome = textOf(row.outcome, '')
    if (outcome) return { text: signalLabel(SIGNAL_OUTCOMES, outcome, outcome), tone: signalOutcomeTone(outcome) }
    if (textOf(row.eval_status, '') === 'unable') return { text: '无法评估', tone: 'text-warning' }
    return { text: '尚未复盘', tone: 'text-muted' }
  }
  if (chosen.length === 1) return describe(chosen[0])
  return {
    text: chosen.map(row => `${signalLabel(SIGNAL_HORIZONS, row.horizon, textOf(row.horizon, '周期'))} ${describe(row).text}`).join('；'),
    tone: 'text-secondary',
  }
}

export function readDecisionSignalList(list: unknown, outcomePages: readonly unknown[] = []) {
  const items = asRecords(list)
  const total = isRecord(list) && typeof list.total === 'number' ? list.total : items.length
  const outcomes = outcomePagesBySignal(outcomePages)
  const rows: DecisionSignalRow[] = items.map(item => {
    const action = textOf(item.action, '')
    const label = textOf(item.action_label, '')
    const direction = /[\u4e00-\u9fff]/.test(label) ? label : signalLabel(SIGNAL_ACTIONS, action, action || '—')
    const horizonKey = textOf(item.horizon, '')
    const id = textOf(item.id, '')
    const outcome = outcomeSummary(id ? outcomes.get(id) ?? [] : [], horizonKey)
    return {
      id: id || `${action}-${horizonKey}-${textOf(item.created_at, '')}`,
      direction,
      directionTone: signalDirectionTone(action),
      horizon: signalLabel(SIGNAL_HORIZONS, horizonKey),
      status: signalLabel(SIGNAL_STATUSES, item.status),
      createdAt: signalTime(item.created_at),
      outcome: outcome.text,
      outcomeTone: outcome.tone,
    }
  })
  return { total, rows }
}

async function loadDecisionSignals(code: string) {
  const list = await dsaUpstream<unknown>(`decision-signals?stock_code=${encodeURIComponent(code)}&page_size=20`)
  const outcomePages = await Promise.all(asRecords(list).slice(0, 20).map(async item => {
    const id = textOf(item.id, '')
    if (!/^\d+$/.test(id)) return null
    try {
      return await dsaUpstream<unknown>(`decision-signals/${id}/outcomes`)
    } catch {
      return null
    }
  }))
  return { list, outcomePages: outcomePages.filter((page): page is unknown => page != null) }
}

export function DecisionSignalsPanel({
  code,
  onCodeChange,
  onSubmit,
  reachable,
  pending,
  error,
  onRetry,
  stats,
  list,
  outcomePages,
}: {
  code: string
  onCodeChange: (value: string) => void
  onSubmit: () => void
  reachable: boolean
  pending: boolean
  error: unknown
  onRetry?: () => void
  stats: unknown
  list: unknown
  outcomePages: readonly unknown[]
}) {
  const parsed = list == null ? null : readDecisionSignalList(list, outcomePages)
  const raw = stats != null || list != null ? { stats, signals: list } : null
  return (
    <Panel title="决策信号" hint="对照事后走势看当时的建议。开启 DSA 管理登录后，这部分需要上游会话。">
      <form className="mb-3 flex gap-2" onSubmit={event => { event.preventDefault(); onSubmit() }}>
        <input className={cn(fieldCls, 'w-36 font-mono')} value={code} onChange={event => onCodeChange(event.target.value)} aria-label="信号代码" />
        <button className={primaryBtn} type="submit" disabled={!reachable || pending}>查看</button>
      </form>
      {error != null && <div className="mb-2"><Failure error={error} onRetry={onRetry} /></div>}
      {!reachable && <EmptyState icon={Sparkles} title="决策服务未连接" />}
      {reachable && pending && list == null && <p className="text-xs text-muted">加载中…</p>}
      {reachable && parsed?.total === 0 && parsed.rows.length === 0 && (
        <EmptyState icon={Sparkles} title="该股暂无决策信号" hint="分析生成后会自动记录。" />
      )}
      {reachable && parsed != null && parsed.rows.length > 0 && (
        <ul className="space-y-2">
          {parsed.rows.map(row => (
            <li key={row.id} data-signal-row className="rounded-lg border border-border bg-base px-3 py-2">
              <div className="flex items-baseline justify-between gap-3">
                <span data-signal-direction className={cn('text-sm font-semibold', row.directionTone)}>{row.direction}</span>
                <span data-signal-status className="shrink-0 text-[11px] text-secondary">{row.status}</span>
              </div>
              <dl className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                <div>
                  <dt className="text-muted">周期</dt>
                  <dd data-signal-horizon className="text-foreground">{row.horizon}</dd>
                </div>
                <div>
                  <dt className="text-muted">记录时间</dt>
                  <dd data-signal-created className="text-foreground">{row.createdAt}</dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-muted">后验</dt>
                  <dd data-signal-outcome className={row.outcomeTone}>{row.outcome}</dd>
                </div>
              </dl>
            </li>
          ))}
        </ul>
      )}
      {raw != null && (
        <details data-signal-raw className="mt-3">
          <summary className="cursor-pointer text-xs text-secondary">原始数据</summary>
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-secondary">{JSON.stringify(raw, null, 2)}</pre>
        </details>
      )}
    </Panel>
  )
}

function Signals({ reachable, initialCode }: { reachable: boolean; initialCode: string }) {
  const [draft, setDraft] = useState(initialCode || '600519')
  const [code, setCode] = useState((initialCode || '600519').trim())
  const stats = useQuery({
    queryKey: QK.dsaUpstream('signal-stats'),
    queryFn: () => dsaUpstream<unknown>('decision-signals/outcomes/stats'),
    enabled: reachable,
    retry: false,
  })
  const signals = useQuery({
    queryKey: QK.dsaUpstream(`decision-signals:${code}`),
    queryFn: () => loadDecisionSignals(code),
    enabled: reachable && code.length > 0,
    retry: false,
  })
  return (
    <DecisionSignalsPanel
      code={draft}
      onCodeChange={setDraft}
      onSubmit={() => {
        const next = draft.trim()
        if (!next) return
        if (next === code) void signals.refetch()
        else setCode(next)
      }}
      reachable={reachable}
      pending={signals.isFetching}
      error={signals.error ?? null}
      onRetry={() => { void signals.refetch() }}
      stats={stats.data}
      list={signals.data?.list}
      outcomePages={signals.data?.outcomePages ?? []}
    />
  )
}

function Portfolio({ sample, reachable }: { sample: boolean; reachable: boolean }) {
  const risk = useQuery({
    queryKey: QK.dsaUpstream('portfolio-risk'),
    queryFn: () => dsaUpstream<unknown>('portfolio/risk'),
    enabled: reachable,
    retry: false,
  })
  const data = reachable ? risk.data : sample ? SAMPLE_RISK : null
  return (
    <Panel title={sample ? '持仓风险 · 样例' : '持仓风险'} hint="DSA 自己的账户账本。持仓提醒和模拟盘的数据不会混进来。">
      {risk.isError && <Failure error={risk.error} onRetry={() => risk.refetch()} />}
      {!reachable && !sample && <EmptyState icon={PieChart} title="决策服务未连接" />}
      {reachable && risk.isPending && risk.data == null && <p className="text-xs text-muted">加载中…</p>}
      {data != null && !risk.isError && (
        <>
          <PortfolioRiskView data={data} />
          <RawJson data={data} />
        </>
      )}
    </Panel>
  )
}

function DecisionBacktest({ reachable, initialCode }: { reachable: boolean; initialCode: string }) {
  const [code, setCode] = useState(initialCode)
  const run = useMutation({
    mutationFn: () => dsaUpstream('backtest/run', {
      method: 'POST',
      body: JSON.stringify({ code: code.trim() || null, eval_window_days: 5, force: false }),
    }),
  })
  return (
    <Panel title="决策回测" hint="用分析之后的行情给历史建议打分。策略页的 T+1 回测不从这里进入。" extra={<span data-tsp-audit-badge className="rounded-btn bg-elevated px-2 py-1 text-[11px] text-secondary">{TSP_AUDIT_BADGE}</span>}>
      <form className="flex gap-2" onSubmit={event => { event.preventDefault(); run.mutate() }}>
        <input className={cn(fieldCls, 'w-36 font-mono')} value={code} placeholder="留空为全部" onChange={event => setCode(event.target.value)} aria-label="回测代码" />
        <button className={primaryBtn} type="submit" disabled={!reachable || run.isPending}>评估历史建议</button>
      </form>
      {run.isError && <div className="mt-2"><Failure error={run.error} /></div>}
      {run.data != null && (
        <>
          <BacktestSummary data={run.data} />
          <RawJson data={run.data} />
        </>
      )}
      <div className="mt-4">
        <LedgerAudit />
      </div>
    </Panel>
  )
}

function ImportCodes({ reachable }: { reachable: boolean }) {
  const [text, setText] = useState('贵州茅台\nhk00700\nAAPL')
  const parseText = useMutation({
    mutationFn: () => dsaUpstream('stocks/parse-import', {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  })
  const parseFile = useMutation({
    mutationFn: (file: File) => {
      const body = new FormData()
      body.append('file', file)
      return dsaUpstream('stocks/parse-import', { method: 'POST', body })
    },
  })
  const result = parseFile.data ?? parseText.data
  return (
    <Panel title="智能导入" hint="识别图片、表格或粘贴文本里的代码。确认后再送到个股研报，不会自动下单。">
      <textarea className="h-28 w-full rounded-lg border border-border bg-base px-3 py-2 text-xs text-foreground outline-none focus:border-accent" value={text} onChange={event => setText(event.target.value)} aria-label="粘贴文本" />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button className={primaryBtn} type="button" disabled={!reachable || parseText.isPending} onClick={() => parseText.mutate()}>解析文本</button>
        <label className={cn(ghostBtn, !reachable && 'pointer-events-none opacity-50')}>
          上传 CSV / Excel / 图片
          <input
            className="sr-only"
            type="file"
            accept=".csv,.xlsx,.xls,image/*"
            onChange={event => {
              const file = event.target.files?.[0]
              if (file) parseFile.mutate(file)
            }}
          />
        </label>
      </div>
      {(parseText.isError || parseFile.isError) && <div className="mt-2"><Failure error={parseText.error ?? parseFile.error} /></div>}
      {result != null && (
        <>
          <ImportSummary data={result} />
          <RawJson data={result} />
        </>
      )}
    </Panel>
  )
}

/**
 * 设置区「漏斗自诊断」只读块。
 *
 * 与 {@link UsageSummary}（上游 LLM token 用量）**分标题、分容器**：
 * `data-usage-summary` 仍只属于 token 用量，本块用 `data-dsa-funnel-diagnostics`。
 * 两者不混名、不互相覆盖（硬约束 X7）。
 *
 * 文案只描述「本机发生过什么」，任何比率/达标结论都不给（无生产采样）。
 */
function FunnelDiagnostics({ reachable }: { reachable: boolean }) {
  const diag = buildFunnelDiagnostics({ reachable })
  const counts = diag.counts
  // L4 分动作明细：让「哪三个动作真的被用了」看得见，而不是只有一个总数。
  // 读的是本地键，没有分母 —— 下面的脚注说清了它不构成比率。
  const actionRows = DSA_FUNNEL_L4_ACTIONS.map(action => ({
    action,
    count: readActionCount(action),
    note: DSA_FUNNEL_L4_ACTION_NOTES[action],
  }))
  const rows: [string, string, string][] = [
    ['L1 启用', counts.enabledSeen ? '曾见启用' : '未见过', DSA_FUNNEL_PENDING_NOTES['tsp.dsa.funnel.enabled_seen']],
    ['L2 可达', `通 ${counts.reachableTrueCount} · 断 ${counts.reachableFalseCount}`, DSA_FUNNEL_PENDING_NOTES['tsp.dsa.funnel.reachable_true_count']],
    ['L3 打开', `${counts.openWorkspaceCount} 次`, DSA_FUNNEL_PENDING_NOTES['tsp.dsa.funnel.open_workspace_count']],
    ['L4 留存动作', `${counts.coreActionCount} 次`, DSA_FUNNEL_PENDING_NOTES['tsp.dsa.funnel.core_action_count']],
  ]
  return (
    <div data-dsa-funnel-diagnostics className="mt-2 space-y-2">
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          ['当前可达', diag.reachabilityLabel],
          ['漏斗状态', diag.status],
          ['L4 状态', diag.l4Status],
          ['口径', '本机 only'],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg border border-border bg-base px-3 py-2">
            <dt className="text-[11px] text-muted">{label}</dt>
            <dd className="text-xs text-foreground">{value}</dd>
          </div>
        ))}
      </dl>
      <ul className="space-y-1">
        {rows.map(([label, value, note]) => (
          <li key={label} className="grid gap-1 text-[11px] sm:grid-cols-[88px_96px_1fr]">
            <span className="text-foreground">{label}</span>
            <span className="num text-secondary">{value}</span>
            <span className="text-muted">{note}</span>
          </li>
        ))}
      </ul>
      <div data-dsa-funnel-l4-actions className="rounded-lg border border-border bg-base px-3 py-2">
        <p className="text-[11px] text-foreground">L4 动作明细（{DSA_FUNNEL_L4_STATUS}）</p>
        <ul className="mt-1 space-y-0.5">
          {actionRows.map(({ action, count, note }) => (
            <li key={action} className="grid gap-1 text-[11px] sm:grid-cols-[112px_48px_1fr]">
              <code className="font-mono text-accent">{action}</code>
              <span className="num text-secondary">{count} 次</span>
              <span className="text-muted">{note}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="text-[10px] text-muted">
        计数只存本机（localStorage，前缀 <code className="font-mono">tsp.dsa.funnel.</code>），不上报、不进买卖信号链路。
        无生产多日采样，故状态为 <code className="font-mono">{diag.status}</code>；L4 动作枚举已钉死，故为{' '}
        <code className="font-mono">{DSA_FUNNEL_L4_STATUS}</code>。此处不给任何比率结论 —— 次数没有分母，推不出使用率。
      </p>
    </div>
  )
}

function SettingsPanel({
  features,
  envVars,
  commit,
  reachable,
  detail,
}: {
  features: { id: string; label: string; overlap: string }[]
  envVars: { name: string; purpose: string }[]
  commit?: string
  reachable: boolean
  detail?: string
}) {
  const usage = useQuery({
    queryKey: QK.dsaUpstream('usage'),
    queryFn: () => dsaUpstream<unknown>('usage/summary'),
    enabled: reachable,
    retry: false,
  })
  return (
    <div className="space-y-3">
      <Panel title="连接" hint={detail || '等待状态'}>
        <p className="text-xs text-secondary">
          上游提交 {commit ? <span className="font-mono">{commit.slice(0, 12)}</span> : '未知'}。
          用量接口在服务可用时显示在下方。
        </p>
        {usage.isError && <div className="mt-2"><Failure error={usage.error} /></div>}
        {usage.data != null && (
          <>
            <UsageSummary data={usage.data} />
            <RawJson data={usage.data} />
          </>
        )}
      </Panel>
      <Panel title="和现有功能怎么共存">
        <ul className="space-y-2">
          {features.map(item => (
            <li key={item.id} className="text-xs leading-relaxed">
              <span className="text-foreground">{item.label}。 </span>
              <span className="text-secondary">{item.overlap}</span>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="启用向导" hint="检查清单 · 四步 · 生产默认关（本清单不替你改 .env、不启容器）">
        <DsaEnablementChecklist />
      </Panel>
      <Panel title="漏斗自诊断" hint="使用率漏斗第 1 步 · 只读 · 与本机 LLM 用量无关">
        <FunnelDiagnostics reachable={reachable} />
      </Panel>
      <Panel title="环境变量" hint="写在仓库根目录 .env。页面不回显密钥。">
        <ul className="divide-y divide-border">
          {envVars.map(item => (
            <li key={item.name} className="grid gap-1 py-2 sm:grid-cols-[220px_1fr]">
              <code className="font-mono text-[11px] text-accent">{item.name}</code>
              <span className="text-xs text-secondary">{item.purpose}</span>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  )
}
