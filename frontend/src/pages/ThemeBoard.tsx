/**
 * 盘后复盘板 —— 知识星球复盘笔记里的「主题表」只读视图。
 *
 * 设计定位：**只读复盘参考**，不是交易面板。
 *  - 三列展示：标题(主题) / 强弱 / 日期。强弱是笔记作者手写的判断，**原样展示**。
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
        subtitle="知识星球复盘笔记里的主题表，只读汇总"
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
            扫描 {data.posts} 篇笔记 · 其中 {data.posts_with_table} 篇含主题表 · 共 {rows.length} 行
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
            <p className="mt-2 text-sm text-zinc-500">
              这段时间的星球笔记里没有解析到主题表。
            </p>
            <p className="mt-1 text-xs text-zinc-600">
              主题表需写成 Markdown 表格，且表头含「主题」列。
            </p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-zinc-800">
            <table className="w-full border-collapse text-sm">
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
                    <td className="px-4 py-2.5 text-zinc-200">{row.theme}</td>
                    <td className={cn('px-4 py-2.5', strengthColor(row.strength))}>
                      {row.strength || <span className="text-zinc-600">—</span>}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-zinc-400">
                      {row.day || <span className="text-zinc-600">—</span>}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-zinc-500">
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
