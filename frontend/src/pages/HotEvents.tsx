import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type NewsCandidate, type NewsSourceHealth } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

const POLL_MS = 60_000

const TABS = [
  { key: 'sector' as const, label: '热门板块' },
  { key: 'stock' as const, label: '热门个股' },
]

function authLabel(source: NewsSourceHealth): string {
  if (!source.configured) return '未配置'
  if (source.auth_state === 'expired') return '登录失效'
  if (source.auth_state === 'missing') return '未找到采集程序'
  if (source.auth_state === 'ok') return '正常'
  if (source.last_error) return '最近失败'
  return '尚未采集'
}

export function HotEvents() {
  const [tab, setTab] = useState<'sector' | 'stock'>('sector')
  const [picked, setPicked] = useState<NewsCandidate | null>(null)
  const queryClient = useQueryClient()

  const hot = useQuery({
    queryKey: QK.newsHot(tab),
    queryFn: () => api.newsHot(tab),
    refetchInterval: POLL_MS,
  })
  const health = useQuery({
    queryKey: QK.newsHealth,
    queryFn: () => api.newsHealth(),
    refetchInterval: POLL_MS,
  })
  const messages = useQuery({
    queryKey: QK.newsMessages(picked?.kind ?? tab, picked?.key ?? ''),
    queryFn: () => api.newsMessages(picked!.kind, picked!.key),
    enabled: picked != null,
  })
  const toggle = useMutation({
    mutationFn: (source: NewsSourceHealth) =>
      api.newsSetSources({ [source.id]: { enabled: !source.enabled } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QK.newsHealth })
    },
  })

  const candidates = hot.data?.candidates ?? []

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="热门事件"
        subtitle="近 24 小时相对前 4 日基线升温的板块和个股。只展示摘录，供内部研究。"
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-auto p-4 lg:grid-cols-[minmax(0,1fr)_280px]">
        <section className="min-w-0">
          <div className="mb-3 flex gap-2">
            {TABS.map(item => (
              <button
                key={item.key}
                type="button"
                className={cn(
                  'rounded-md border px-3 py-1.5 text-sm',
                  tab === item.key
                    ? 'border-accent bg-accent/10 text-foreground'
                    : 'border-border text-muted',
                )}
                onClick={() => {
                  setTab(item.key)
                  setPicked(null)
                }}
              >
                {item.label}
              </button>
            ))}
          </div>

          {hot.isLoading && <p className="text-sm text-muted">加载中…</p>}
          {hot.isError && <p className="text-sm text-danger">热门候选加载失败</p>}
          {hot.isSuccess && candidates.length === 0 && (
            <p className="text-sm text-muted">
              还没有候选。打开右侧来源并等待采集后，这里会列出升温的板块和个股。
            </p>
          )}

          <ul className="space-y-2">
            {candidates.map(item => (
              <li key={item.key}>
                <button
                  type="button"
                  className={cn(
                    'w-full rounded-md border px-3 py-2 text-left',
                    picked?.key === item.key ? 'border-accent' : 'border-border',
                  )}
                  onClick={() => setPicked(item)}
                >
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="font-medium">{item.name}</span>
                    <span className="text-xs text-muted">分数 {item.score}</span>
                  </div>
                  <div className="mt-1 text-xs text-muted">
                    {item.story_count} 条故事 · {item.sources.length} 个来源 · 相对基线 {item.growth} 倍
                  </div>
                </button>
              </li>
            ))}
          </ul>

          {picked && (
            <div className="mt-4 border-t border-border pt-3">
              <h2 className="mb-2 text-sm font-medium">{picked.name} 的相关摘录</h2>
              {messages.isLoading && <p className="text-sm text-muted">加载摘录…</p>}
              {messages.isError && <p className="text-sm text-danger">摘录加载失败</p>}
              {messages.isSuccess && messages.data.items.length === 0 && (
                <p className="text-sm text-muted">这个窗口里没有可展示的摘录。</p>
              )}
              <ul className="space-y-3">
                {(messages.data?.items ?? []).map(item => (
                  <li key={`${item.source}-${item.published_at}-${item.title}`} className="text-sm">
                    <div className="text-xs text-muted">
                      {item.source_label} · {item.published_at}
                      {item.level ? ` · ${item.level}` : ''}
                    </div>
                    {item.title && <div className="font-medium">{item.title}</div>}
                    <p className="text-secondary">{item.excerpt}</p>
                    {item.url && (
                      <a className="text-xs text-accent" href={item.url} target="_blank" rel="noreferrer">
                        打开来源链接
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>

        <aside className="h-fit rounded-md border border-border p-3">
          <h2 className="mb-2 text-sm font-medium">采集来源</h2>
          {health.isLoading && <p className="text-sm text-muted">加载中…</p>}
          {health.isError && <p className="text-sm text-danger">来源状态加载失败</p>}
          <ul className="space-y-3">
            {(health.data?.sources ?? []).map(source => (
              <li key={source.id} className="text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span>{source.label}</span>
                  <button
                    type="button"
                    disabled={source.locked || !source.configured || toggle.isPending}
                    className={cn(
                      'rounded border px-2 py-0.5 text-xs',
                      source.enabled ? 'border-accent text-foreground' : 'border-border text-muted',
                    )}
                    onClick={() => toggle.mutate(source)}
                  >
                    {!source.configured ? '未配置' : source.enabled ? '已开启' : '已关闭'}
                  </button>
                </div>
                <div className="mt-1 text-xs text-muted">
                  {authLabel(source)}
                  {source.locked ? ' · 由环境变量锁定' : ''}
                  {source.items_ingested ? ` · 已入库 ${source.items_ingested}` : ''}
                </div>
                {source.last_error && (
                  <div className="mt-1 text-xs text-danger">{source.last_error}</div>
                )}
              </li>
            ))}
          </ul>
        </aside>
      </div>
    </div>
  )
}
