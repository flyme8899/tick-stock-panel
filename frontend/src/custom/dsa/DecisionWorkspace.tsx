import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
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
  ShieldAlert,
  Sparkles,
  Upload,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import {
  asRecords,
  sampleRecords,
  dsaCommand,
  dsaEtfRotation,
  dsaUpstream,
  fetchDsaCatalog,
  fetchDsaQuantEvidence,
  fetchDsaSchedule,
  fetchDsaStatus,
  fetchShareImage,
  saveDsaSchedule,
  isRecord,
  textOf,
  type DsaError,
} from './client'
import {
  SAMPLE_ALERTS,
  SAMPLE_HISTORY,
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
const ghostBtn = 'inline-flex h-8 items-center gap-1.5 rounded-btn bg-elevated px-3 text-xs text-secondary hover:text-foreground disabled:opacity-50'
const fieldCls = 'h-8 rounded-btn border border-border bg-base px-2.5 text-xs text-foreground outline-none focus:border-accent'

function adviceClass(advice: string) {
  if (/买/.test(advice)) return 'text-bull'
  if (/卖/.test(advice)) return 'text-bear'
  if (/观望|持有/.test(advice)) return 'text-warning'
  return 'text-secondary'
}

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

  const select = (id: SectionId) => {
    const next = new URLSearchParams(params)
    next.set('section', id)
    setParams(next, { replace: true })
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-base">
      <PageHeader
        title="决策"
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
        <div className="min-h-0 min-w-0 flex-1 overflow-auto px-4 py-4 md:px-6">
          {sample && (
            <div className="mb-3">
              <Notice>下面带「样例」标记的内容只用于查看版式，不是行情，也不能当作交易依据。连接决策服务并配置模型密钥后会换成真实结果。</Notice>
            </div>
          )}
          {section === 'dashboard' && <Dashboard sample={sample} reachable={reachable} />}
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

function Dashboard({ sample, reachable }: { sample: boolean; reachable: boolean }) {
  const query = useQuery({
    queryKey: QK.dsaUpstream('history'),
    queryFn: () => dsaUpstream<unknown>('history?limit=12'),
    enabled: reachable,
    retry: false,
  })
  const rows = reachable ? asRecords(query.data) : sample ? SAMPLE_HISTORY : []
  const counts = useMemo(() => {
    const bucket = { buy: 0, hold: 0, sell: 0 }
    for (const row of rows) {
      const advice = textOf(row.operation_advice, '')
      if (/买/.test(advice)) bucket.buy += 1
      else if (/卖/.test(advice)) bucket.sell += 1
      else bucket.hold += 1
    }
    return bucket
  }, [rows])

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="买入" value={counts.buy} tone="text-bull" />
        <Stat label="观望" value={counts.hold} tone="text-warning" />
        <Stat label="卖出" value={counts.sell} tone="text-bear" />
      </div>
      <QuantEvidence />
      <Panel
        title={sample ? '今日决策 · 样例' : '今日决策'}
        hint="手动分析和定时任务的结论都在这里。定时结果同时按 .env 里的通知渠道推送。"
        extra={<MarketReview reachable={reachable} />}
      >
        {query.isError && <Failure error={query.error} onRetry={() => query.refetch()} />}
        {!query.isError && rows.length === 0 && (
          <Notice>{reachable ? '还没有分析记录。到「个股研报」提交一只股票，或等定时任务跑完。' : '决策服务未连接。'}</Notice>
        )}
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          {rows.map(row => (
            <article key={textOf(row.id, textOf(row.stock_code))} className="rounded-lg border border-border bg-base px-3 py-2.5">
              <div className="flex items-baseline justify-between gap-2">
                <div className="min-w-0">
                  <span className="text-sm text-foreground">{textOf(row.stock_name, textOf(row.stock_code))}</span>
                  <span className="ml-2 font-mono text-[11px] text-muted">{textOf(row.stock_code)}</span>
                </div>
                <span className={cn('text-xs font-medium', adviceClass(textOf(row.operation_advice, '')))}>
                  {textOf(row.operation_advice)}
                </span>
              </div>
              <div className="mt-1 flex gap-3 text-[11px] text-muted">
                <span>评分 <b className="num text-foreground">{textOf(row.sentiment_score)}</b></span>
                <span>{textOf(row.trend_prediction)}</span>
                <span className="uppercase">{textOf(row.region, '')}</span>
              </div>
              <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-secondary">{textOf(row.analysis_summary, '')}</p>
            </article>
          ))}
        </div>
      </Panel>
    </div>
  )
}

function formatSignedPercent(value: number): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
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
  const scope = [
    data.meta.period,
    data.meta.universe != null ? `全市场 ${data.meta.universe} 只` : '',
    data.meta.benchmark_return ? `同期基准 ${data.meta.benchmark_return}` : '',
  ].filter(Boolean).join('，')
  return (
    <Panel
      title="量化回测参考"
      hint={scope ? `${scope}。同名技能的提示里会附上这些机械回测结果。` : '同名技能的提示里会附上这些机械回测结果。'}
    >
      <Notice>{data.caveat}</Notice>
      <ul className="mt-3 space-y-2">
        {data.skills.map(skill => (
          <li key={skill.name} className="text-xs leading-relaxed text-secondary">
            <span className="font-mono text-foreground">{skill.name}</span>
            {skill.matched.length > 0 && <span className="text-muted"> · {skill.matched.join('、')}</span>}
            {skill.returns.length > 0 && (
              <span className="num ml-2 text-foreground">{skill.returns.map(formatSignedPercent).join(' / ')}</span>
            )}
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function Stat({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={cn('num mt-1 text-2xl', tone)}>{value}</div>
    </div>
  )
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

function Reports({ sample, reachable, initialCode }: { sample: boolean; reachable: boolean; initialCode: string }) {
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
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.dsaUpstream('history') }) },
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
    </div>
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
      {rows.length === 0 && !query.isError && <Notice>{reachable ? '情报库是空的。先在上游配置搜索源，再点立即拉取。' : '决策服务未连接。'}</Notice>}
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
        {query.data != null && <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-secondary">{JSON.stringify(query.data, null, 2)}</pre>}
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
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-muted">
            <tr><th className="py-1 font-normal">代码</th><th className="font-normal">名称</th><th className="font-normal">市场</th><th className="font-normal">分数</th><th className="font-normal">说明</th></tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr key={textOf(row.code, textOf(row.stock_code))} className="border-t border-border">
                <td className="py-1.5 font-mono">{textOf(row.code, textOf(row.stock_code))}</td>
                <td>{textOf(row.name, textOf(row.stock_name))}</td>
                <td className="uppercase">{textOf(row.market, '')}</td>
                <td className="num">{textOf(row.score, '')}</td>
                <td className="text-secondary">{textOf(row.reason, textOf(row.summary, ''))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length === 0 && <Notice>还没有筛选结果。</Notice>}
    </Panel>
  )
}

function EtfRotation() {
  const run = useMutation({ mutationFn: dsaEtfRotation })
  const detail = isRecord(run.data) ? textOf(run.data.detail, '') : ''
  return (
    <Panel title="ETF 双动量" hint="不调用大模型。股票池、避险资产和换仓周期读 ETF_ROTATION_* 环境变量。">
      <button className={primaryBtn} type="button" disabled={run.isPending} onClick={() => run.mutate()}>运行轮动</button>
      {run.isError && <div className="mt-2"><Failure error={run.error} /></div>}
      {detail && <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-secondary">{detail}</pre>}
      {!detail && <p className="mt-3 text-xs text-muted">未安装 sidecar 时会提示如何启动，不会调用系统里别的 Python。</p>}
    </Panel>
  )
}

function Chat({ reachable }: { reachable: boolean }) {
  const [skill, setSkill] = useState('')
  const [input, setInput] = useState('')
  const [lines, setLines] = useState<{ role: 'user' | 'assistant'; text: string }[]>([])
  const send = useMutation({
    mutationFn: (message: string) => dsaUpstream<Record<string, unknown>>('agent/chat', {
      method: 'POST',
      body: JSON.stringify({ message, skills: skill.trim() ? [skill.trim()] : undefined }),
    }),
    onSuccess: data => {
      const text = ['reply', 'content', 'message', 'text'].map(key => data[key]).find(item => typeof item === 'string') as string | undefined
      setLines(current => [...current, { role: 'assistant', text: text || JSON.stringify(data, null, 2) }])
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
          {lines.map((line, index) => (
            <pre key={`${line.title}-${index}`} className="overflow-auto whitespace-pre-wrap rounded-lg bg-base px-3 py-2 font-mono text-[11px] leading-relaxed text-secondary">
              {line.title}{'\n'}{line.text}
            </pre>
          ))}
        </div>
      </Panel>
    </div>
  )
}

function Schedule({ reachable }: { reachable: boolean }) {
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
  const [channel, setChannel] = useState('feishu')
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
            <option value="feishu">飞书</option>
            <option value="wechat">企业微信</option>
            <option value="telegram">Telegram</option>
            <option value="discord">Discord</option>
            <option value="slack">Slack</option>
            <option value="email">邮件</option>
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
      {rows.length === 0 && !query.isError && <Notice>还没有决策预警。</Notice>}
      <ul className="divide-y divide-border">
        {rows.map((row, index) => (
          <li key={`${textOf(row.name, 'rule')}-${index}`} className="flex items-center justify-between gap-3 py-2 text-xs">
            <span className="text-foreground">{textOf(row.name, textOf(row.title))}</span>
            <span className="font-mono text-muted">{textOf(row.symbol, textOf(row.stock_code, ''))}</span>
            <span className="text-secondary">{textOf(row.status, textOf(row.enabled, ''))}</span>
            <span className="text-muted">{textOf(row.last, textOf(row.last_triggered_at, ''))}</span>
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function Signals({ reachable, initialCode }: { reachable: boolean; initialCode: string }) {
  const [code, setCode] = useState(initialCode || '600519')
  const stats = useQuery({
    queryKey: QK.dsaUpstream('signal-stats'),
    queryFn: () => dsaUpstream<unknown>('decision-signals/outcomes/stats'),
    enabled: reachable,
    retry: false,
  })
  const latest = useMutation({
    mutationFn: () => dsaUpstream(`decision-signals/latest/${encodeURIComponent(code.trim())}`),
  })
  return (
    <Panel title="决策信号" hint="对照事后走势看当时的建议。开启 DSA 管理登录后，这部分需要上游会话。">
      <form className="mb-3 flex gap-2" onSubmit={event => { event.preventDefault(); latest.mutate() }}>
        <input className={cn(fieldCls, 'w-36 font-mono')} value={code} onChange={event => setCode(event.target.value)} aria-label="信号代码" />
        <button className={primaryBtn} type="submit" disabled={!reachable || latest.isPending}>查看最新</button>
      </form>
      {stats.isError && <Failure error={stats.error} onRetry={() => stats.refetch()} />}
      {latest.isError && <div className="mb-2"><Failure error={latest.error} /></div>}
      {stats.data != null && <pre className="mb-2 max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-secondary">{JSON.stringify(stats.data, null, 2)}</pre>}
      {latest.data != null && <pre className="max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-secondary">{JSON.stringify(latest.data, null, 2)}</pre>}
    </Panel>
  )
}

function Portfolio({ sample, reachable }: { sample: boolean; reachable: boolean }) {
  const risk = useQuery({
    queryKey: QK.dsaUpstream('portfolio-risk'),
    queryFn: () => dsaUpstream<unknown>('portfolio/risk'),
    enabled: reachable,
    retry: false,
  })
  const rows = reachable ? asRecords(risk.data) : sample ? sampleRecords(SAMPLE_RISK) : []
  return (
    <Panel title={sample ? '持仓风险 · 样例' : '持仓风险'} hint="DSA 自己的账户账本。持仓提醒和模拟盘的数据不会混进来。">
      {risk.isError && <Failure error={risk.error} onRetry={() => risk.refetch()} />}
      {rows.length === 0 && !risk.isError && risk.data == null && <Notice>{reachable ? '还没有组合账户。可在上游导入成交。' : '决策服务未连接。'}</Notice>}
      {rows.length > 0 && (
        <ul className="divide-y divide-border">
          {rows.map((row, index) => (
            <li key={`${textOf(row.name)}-${index}`} className="py-2 text-xs">
              <div className="flex justify-between gap-3">
                <span className="text-foreground">{textOf(row.name, textOf(row.title))}</span>
                <span className="text-warning">{textOf(row.level, '')}</span>
              </div>
              <p className="mt-0.5 text-muted">{textOf(row.detail, textOf(row.summary, ''))}</p>
            </li>
          ))}
        </ul>
      )}
      {rows.length === 0 && risk.data != null && (
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-secondary">{JSON.stringify(risk.data, null, 2)}</pre>
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
    <Panel title="决策回测" hint="用分析之后的行情给历史建议打分。策略页的 T+1 回测不从这里进入。">
      <form className="flex gap-2" onSubmit={event => { event.preventDefault(); run.mutate() }}>
        <input className={cn(fieldCls, 'w-36 font-mono')} value={code} placeholder="留空为全部" onChange={event => setCode(event.target.value)} aria-label="回测代码" />
        <button className={primaryBtn} type="submit" disabled={!reachable || run.isPending}>评估历史建议</button>
      </form>
      {run.isError && <div className="mt-2"><Failure error={run.error} /></div>}
      {run.data != null && <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-secondary">{JSON.stringify(run.data, null, 2)}</pre>}
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
      {result != null && <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-secondary">{JSON.stringify(result, null, 2)}</pre>}
    </Panel>
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
        {usage.data != null && <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-secondary">{JSON.stringify(usage.data, null, 2)}</pre>}
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
