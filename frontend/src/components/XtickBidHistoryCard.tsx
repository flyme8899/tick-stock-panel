import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Database } from 'lucide-react'
import { api, type XtickBidHistoryPayload, type XtickBidHistoryRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'

/**
 * 异动页上的 XTick 历史竞价补充。手动查询，不替换 fuyao 风向标或竞价扫描。
 */
export function XtickBidHistoryCard({ onOpenStock }: {
  onOpenStock?: (symbol: string) => void
}) {
  const status = useQuery({
    queryKey: QK.xtickSupplementStatus,
    queryFn: () => api.xtickSupplementStatus(),
    staleTime: 30_000,
    retry: false,
  })
  const [code, setCode] = useState('000001')
  const [tradeDate, setTradeDate] = useState('')
  const [seq, setSeq] = useState('')
  const [submitted, setSubmitted] = useState<{ code: string; tradeDate: string; seq: string } | null>(null)

  const history = useQuery({
    queryKey: QK.xtickBidHistory(submitted?.code ?? '', submitted?.tradeDate ?? '', submitted?.seq ?? ''),
    queryFn: () => api.xtickBidHistory(submitted!),
    enabled: submitted != null,
    retry: false,
  })

  const enabled = status.data?.enabled === true
  const credential = status.data?.credential_configured === true
  const canQuery = enabled && credential && code.trim().length > 0 && tradeDate.length > 0

  return (
    <section data-xtick-card className="rounded-card border border-border bg-surface/80 px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded bg-elevated">
            <Database className="h-4 w-4 text-muted" />
          </span>
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-foreground">XTick 历史竞价补充</h3>
            <p className="text-xs leading-relaxed text-muted">
              来源 XTick · 历史终态 · 不是实时行情 · 不参与评分、风控或交易触发
            </p>
          </div>
        </div>
        <span className="rounded border border-border px-2 py-0.5 text-[11px] text-secondary">补充数据</span>
      </div>

      {status.isLoading && <p className="mt-3 text-xs text-muted">正在读取补充源开关…</p>}
      {status.isError && (
        <p className="mt-3 text-xs text-secondary">补充源状态暂时无法读取。主列表不受影响。</p>
      )}
      {status.data && !enabled && (
        <p className="mt-3 text-xs leading-relaxed text-secondary">
          补充源未开启。含本功能的容器跑起来之后，只在 secrets.json 增加 xtick_supplement_enabled=1 即可，不必改 .env。环境变量写成 0 会强制关闭。
        </p>
      )}
      {status.data && enabled && !credential && (
        <p className="mt-3 text-xs leading-relaxed text-secondary">
          已开启但未配置凭据。把凭据写入 secrets.json 的 xtick_api_token。页面不保存、不显示凭据。
        </p>
      )}

      {enabled && credential && (
        <form
          className="mt-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-end"
          onSubmit={(event) => {
            event.preventDefault()
            if (!canQuery) return
            setSubmitted({ code: code.trim(), tradeDate, seq })
          }}
        >
          <label className="flex min-w-0 flex-1 flex-col gap-1 text-[11px] text-muted">
            股票代码
            <input
              aria-label="XTick 股票代码"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              inputMode="numeric"
              className="h-8 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent"
            />
          </label>
          <label className="flex flex-col gap-1 text-[11px] text-muted">
            交易日
            <input
              aria-label="XTick 交易日"
              type="date"
              value={tradeDate}
              onChange={(event) => setTradeDate(event.target.value)}
              className="h-8 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent"
            />
          </label>
          <label className="flex flex-col gap-1 text-[11px] text-muted">
            seq
            <select
              aria-label="XTick seq"
              value={seq}
              onChange={(event) => setSeq(event.target.value)}
              className="h-8 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent"
            >
              <option value="">不传，沿用上游默认</option>
              <option value="0">0</option>
              <option value="1">1</option>
            </select>
          </label>
          <button
            type="submit"
            disabled={!canQuery || history.isFetching}
            className="h-8 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-50"
          >
            查询历史竞价
          </button>
        </form>
      )}

      <p className="mt-2 text-[11px] leading-relaxed text-muted">
        seq 只按原数字请求和展示，官方没有一套可以采信的中文含义。全市场 all 默认关闭。
      </p>

      {history.isFetching && <p className="mt-3 text-xs text-muted">正在向 XTick 读取该日终态…</p>}
      {history.isError && (
        <p className="mt-3 text-xs text-secondary">补充数据暂时无法读取。主列表不受影响。</p>
      )}
      {history.data && <HistoryBody payload={history.data} onOpenStock={onOpenStock} />}
    </section>
  )
}

function HistoryBody({ payload, onOpenStock }: {
  payload: XtickBidHistoryPayload
  onOpenStock?: (symbol: string) => void
}) {
  const rows = payload.rows ?? []
  return (
    <div className="mt-3 flex flex-col gap-2">
      <p className="text-xs leading-relaxed text-secondary">
        <span className="mr-2 font-medium text-foreground">{freshnessLabel(payload.freshness)}</span>
        {payload.trade_date ? `数据日期 ${payload.trade_date}` : ''}
        {payload.cache === 'hit' ? ' · 本地缓存' : ''}
        {payload.fetched_at ? ` · 取得于 ${payload.fetched_at}` : ''}
      </p>
      {payload.message && <p className="text-xs leading-relaxed text-muted">{payload.message}</p>}
      {payload.error?.message && payload.error.message !== payload.message && (
        <p className="text-xs leading-relaxed text-secondary">{payload.error.message}</p>
      )}
      {payload.warnings?.includes('vendor_time_date_mismatch') && (
        <p className="text-xs leading-relaxed text-secondary">
          上游时间戳的日历日和查询日不一致。仍按查询日标注，没有改写成时间戳上的那一天，也没有当成实时行情。
        </p>
      )}
      {payload.unit_note && <p className="text-[11px] leading-relaxed text-muted">{payload.unit_note}</p>}
      {payload.seq_doc?.conflict && (
        <div className="text-[11px] leading-relaxed text-muted">
          <p>seq 未判定。这一列只显示原数字，下面两句官方原文互相矛盾，不能用来解释这一列。</p>
          <p>原文甲：{payload.seq_doc.range_text}</p>
          <p>原文乙：{payload.seq_doc.description_head}</p>
        </div>
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-left text-xs">
            <thead className="text-[11px] text-muted">
              <tr>
                <th className="py-1 pr-3 font-medium">代码</th>
                <th className="py-1 pr-3 font-medium">seq 原值</th>
                <th className="py-1 pr-3 font-medium">上游时间</th>
                <th className="py-1 pr-3 font-medium">价格</th>
                <th className="py-1 pr-3 font-medium">前收</th>
                <th className="py-1 pr-3 font-medium">竞价涨幅</th>
                <th className="py-1 pr-3 font-medium">竞价量</th>
                <th className="py-1 pr-3 font-medium">竞价额</th>
                <th className="py-1 pr-3 font-medium">未匹配量</th>
                <th className="py-1 pr-3 font-medium">未匹配额</th>
                <th className="py-1 font-medium">方向</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <RowView key={`${row.code ?? 'row'}-${row.seq ?? index}`} row={row} onOpenStock={onOpenStock} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function RowView({ row, onOpenStock }: {
  row: XtickBidHistoryRow
  onOpenStock?: (symbol: string) => void
}) {
  const label = row.code ?? '—'
  return (
    <tr className="border-t border-border/70 align-top">
      <td className="py-1.5 pr-3">
        {row.symbol && onOpenStock ? (
          <button type="button" className="font-mono text-accent hover:underline" onClick={() => onOpenStock(row.symbol!)}>
            {label}
          </button>
        ) : (
          <span className="font-mono">{label}</span>
        )}
      </td>
      <td className="py-1.5 pr-3 font-mono">{row.seq ?? '—'}</td>
      <td className="py-1.5 pr-3 font-mono">
        {row.time_bj ?? '—'}
        {row.time_is_midnight && (
          <span className="mt-0.5 block text-[11px] text-muted">时间是 00:00，不解释成 09:25</span>
        )}
        {row.time_matches_trade_date === false && (
          <span className="mt-0.5 block text-[11px] text-muted">与查询日不一致</span>
        )}
      </td>
      <td className="py-1.5 pr-3 font-mono">{fmtRaw(row.price)}</td>
      <td className="py-1.5 pr-3 font-mono">{fmtRaw(row.prev_close)}</td>
      <td className={cn('py-1.5 pr-3 font-mono', priceColorClass(row.jjzf_percent))}>{fmtJjzf(row.jjzf_percent)}</td>
      <td className="py-1.5 pr-3 font-mono">{fmtRaw(row.jjl)}</td>
      <td className="py-1.5 pr-3 font-mono">{fmtRaw(row.jje)}</td>
      <td className="py-1.5 pr-3 font-mono">{fmtRaw(row.nol)}</td>
      <td className="py-1.5 pr-3 font-mono">{fmtRaw(row.noe)}</td>
      <td className="py-1.5">{row.trend_label ?? (row.trend == null ? '—' : String(row.trend))}</td>
    </tr>
  )
}

function freshnessLabel(freshness: string | undefined): string {
  if (freshness === 'historical') return '历史记录'
  if (freshness === 'same_day_final') return '当日终态，不是实时'
  if (freshness === 'not_ready') return '尚未到可查询时间'
  if (freshness === 'unknown') return '未取得上游记录'
  return '补充记录'
}

function fmtRaw(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const negative = value < 0
  const [whole, frac] = Math.abs(value).toString().split('.')
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${negative ? '-' : ''}${grouped}${frac ? `.${frac}` : ''}`
}

function fmtJjzf(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}
