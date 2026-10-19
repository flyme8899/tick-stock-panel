/**
 * 盘后复盘板 —— 知识星球复盘笔记里的主题，只读视图。
 *
 * 设计定位：**只读复盘参考**，不是交易面板。
 *  - 展示主题 / 强弱 / 日期 / 来源。强弱是笔记作者手写的判断，**原样展示**；
 *    散文笔记没有强弱列时留空。要点摘要只作阅读辅助。
 *  - **不接任何买卖信号、做T 入口或下单提示**（产品明确要求，勿加）。
 *  - 不强弱映射成方向：只在文字颜色上区分 强→红 / 弱→绿（A 股红涨绿跌），
 *    认不出或为空时用中性灰，不做打分、不排序。
 *
 * 数据来源：GET /api/news/theme-board
 */
import { useQuery } from '@tanstack/react-query'
import { RefreshCw, Info, FileText } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { useState } from 'react'

/** 时间窗选项：盘后复盘一般看最近几天到一个月。 */
const RANGE_OPTIONS: { days: number; label: string }[] = [
  { days: 3, label: '近 3 天' },
  { days: 7, label: '近 7 天' },
  { days: 30, label: '近 30 天' },
]

/**
 * 强弱文字 → 颜色（A 股口径：强=红、弱=绿）。
 *
 * 刻意保持保守：只在**同时出现**「强」或「弱」字时才染色；
 * 「分歧」「一致」「震荡」这类无方向描述保持中性灰，避免把作者的
 * 模糊表述过度解读成方向。
 */
function strengthColor(strength: string): string {
  const text = strength.trim()
  if (!text) return 'text-zinc-500'
  const hasStrong = text.includes('强')
  const hasWeak = text.includes('弱')
  if (hasStrong && !hasWeak) return 'text-[#F04438]' // 红 = 强
  if (hasWeak && !hasStrong) return 'text-[#12B76A]' // 绿 = 弱
  return 'text-zinc-400' // 无明确方向（含「弱转强」这类转折）
}

function sourceKindLabel(kind: string | undefined): string {
  if (kind === 'table') return '表格'
  if (kind === 'prose') return '笔记'
  if (kind === 'mixed') return '表格+笔记'
  return ''
}

export function ThemeBoard() {
  const [days, setDays] = useState(7)
  const query = useQuery({
    queryKey: QK.newsThemeBoard(days),
    queryFn: () => api.newsThemeBoard(days),
    staleTime: 5 * 60_000,
  })

  const data = query.data
  const rows = data?.rows ?? []

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6">
      <PageHeader
        title="盘后复盘板"
        subtitle="知识星球复盘笔记里的主题，只读汇总"
        right={
          <button
            type="button"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
            className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 transition-colors hover:border-zinc-500 hover:text-zinc-100 disabled:opacity-50"
          >
            <RefreshCw className={cn('h-4 w-4', query.isFetching && 'animate-spin')} />
            刷新
          </button>
        }
      />

      {/* 只读声明：醒目且常驻，避免被当成信号面板 */}
      <div className="mt-4 flex items-start gap-2 rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2.5 text-sm text-zinc-400">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-zinc-500" />
        <div>
          <p>{data?.note || '只读复盘视图；强弱为笔记原文，非买卖信号'}</p>
          <p className="mt-1 text-xs text-zinc-500">
            本页仅用于盘后复盘参考，不构成任何买卖建议，也不接入做T或信号流程。
          </p>
        </div>
      </div>

      {/* 时间窗 + 统计 */}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {RANGE_OPTIONS.map(option => (
          <button
            key={option.days}
            type="button"
            onClick={() => setDays(option.days)}
            className={cn(
              'rounded-md border px-3 py-1.5 text-sm transition-colors',
              days === option.days
                ? 'border-zinc-500 bg-zinc-800 text-zinc-100'
                : 'border-zinc-800 text-zinc-400 hover:border-zinc-600 hover:text-zinc-200',
            )}
          >
            {option.label}
          </button>
        ))}
        {data && (
          <span className="ml-auto text-xs text-zinc-500">
            扫描 {data.posts} 篇知识星球笔记 · 解析出主题 {data.posts_parsed ?? 0} 篇 · 共 {rows.length} 行
          </span>
        )}
      </div>

      {/* 内容 */}
      <div className="mt-4">
        {query.isLoading ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/30 px-4 py-10 text-center text-sm text-zinc-500">
            正在读取复盘笔记…
          </div>
        ) : query.isError ? (
          <div className="rounded-lg border border-red-900/50 bg-red-950/20 px-4 py-10 text-center text-sm text-red-400">
            读取失败：{(query.error as Error)?.message || '未知错误'}
          </div>
        ) : rows.length === 0 ? (
          <div className="rounded-lg border border-zinc-800 bg-zinc-900/30 px-4 py-10 text-center">
            <FileText className="mx-auto h-6 w-6 text-zinc-600" />
            <p className="mt-2 text-sm text-zinc-300">
              当前时间段没有可解析的复盘笔记。
            </p>
            <p className="mx-auto mt-2 max-w-lg text-xs leading-relaxed text-zinc-500">
              数据来自知识星球复盘笔记（例如「每日投研」「纳指星球调研」）。
              能进入本表的，是标题写成「复盘笔记：主题A/主题B」的帖子，以及正文「强势板块」段落里单独成行的主题名。
            </p>
            <p className="mx-auto mt-2 max-w-lg text-xs leading-relaxed text-zinc-600">
              「涨停板复盘」「雁阵图」多为图片帖，正文几乎为空，暂不支持。
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-zinc-800">
            <table className="w-full min-w-[40rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-zinc-800 bg-zinc-900/50 text-left text-xs text-zinc-400">
                  <th className="px-4 py-2.5 font-medium">主题</th>
                  <th className="w-32 px-4 py-2.5 font-medium">强弱</th>
                  <th className="w-32 px-4 py-2.5 font-medium">日期</th>
                  <th className="px-4 py-2.5 font-medium">来源</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <tr
                    key={`${row.theme}-${row.day}-${index}`}
                    className="border-b border-zinc-900 last:border-0 hover:bg-zinc-900/40"
                  >
                    <td className="px-4 py-2.5 text-zinc-200">
                      <div className="flex flex-wrap items-center gap-2">
                        <span>{row.theme}</span>
                        {sourceKindLabel(row.source_kind) && (
                          <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-normal text-zinc-400">
                            {sourceKindLabel(row.source_kind)}
                          </span>
                        )}
                      </div>
                      {row.summary ? (
                        <p className="mt-1 text-xs leading-relaxed text-zinc-500">{row.summary}</p>
                      ) : null}
                    </td>
                    <td className={cn('px-4 py-2.5', strengthColor(row.strength))}>
                      {row.strength || <span className="text-zinc-600">—</span>}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-zinc-400">
                      {row.day || <span className="text-zinc-600">—</span>}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-zinc-500 break-words">
                      {row.url ? (
                        <a
                          href={row.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="hover:text-zinc-300 hover:underline"
                        >
                          {row.source || '知识星球'}
                        </a>
                      ) : (
                        row.source || '知识星球'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
