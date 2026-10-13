import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { Check, ChevronDown, Minus, Search } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useIsDesktop } from '@/lib/useMediaQuery'

export interface SourceOption {
  id: string
  name: string
  description?: string
  mentions?: number
  sourceCount?: number
  redCount?: number
  updatedAt?: string
  concepts?: string[]
  headline?: string | null
  subtitle?: string
  category?: string
  direction?: string
  importance?: string
  confirmationLabel?: string
  mappedStocks?: string[]
  /** 真实可选股数(与实跑同一把尺)。0 表示这条事件当前选不出任何个股。这是**上限**。 */
  selectableCount?: number
  /** 映射广度：usable=有区分度 / broad=命中过宽 / none=选不出股 / undefined=后端未透出。 */
  mappingBreadth?: 'usable' | 'broad' | 'none' | string
  /** 选不出股的原因(后端 note)。用来区分「维表没同步」和「事件真没股」。 */
  selectableNote?: string | null
  /** 可选数是否被 30 只上限截断。截断了要说「至少 N 只」而不是「约 N 只」。 */
  selectableCapped?: boolean
}

function selectableBadge(item: SourceOption): { text: string; className: string } | null {
  const count = item.selectableCount
  if (count == null) return null
  if (count <= 0) {
    // 原因由后端 note 给出: 维表未同步 / 事件无映射 / 代码对不上, 三种不该长一个样。
    return { text: item.selectableNote || '暂无对应个股', className: 'bg-muted/15 text-muted' }
  }
  if (item.mappingBreadth === 'broad') {
    return { text: '范围较大', className: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' }
  }
  // 被 30 只上限截断时, N 是「至少」不是「约」——不能让人以为这就是全部可选范围。
  if (item.selectableCapped) {
    return { text: `至少 ${count} 只`, className: 'bg-accent/15 text-accent' }
  }
  return { text: `${count} 只`, className: 'bg-accent/15 text-accent' }
}

interface Props {
  label: string
  options: SourceOption[]
  selectedIds: string[]
  onToggle: (id: string) => void
  badge?: string
  hint?: string
  empty?: ReactNode
  footer?: ReactNode
  disabled?: boolean
  searchable?: boolean
  hintClassName?: string
  /** 面板从关闭变为打开时调用。用来在列表为空或上次加载失败时重新请求。 */
  onOpen?: () => void
}

/** 带搜索的多选。桌面展开为下拉，窄屏展开为底部面板。 */
export function SourceDropdown({
  label, options, selectedIds, onToggle, badge, hint, empty, footer, disabled,
  searchable = true, hintClassName, onOpen,
}: Props) {
  const onOpenRef = useRef(onOpen)
  onOpenRef.current = onOpen
  const desktop = useIsDesktop()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const listId = useId()
  const selected = useMemo(() => new Set(selectedIds), [selectedIds])
  const visible = useMemo(() => {
    const text = query.trim().toLowerCase()
    if (!text) return options
    return options.filter(item =>
      item.name.toLowerCase().includes(text) || item.id.toLowerCase().includes(text))
  }, [options, query])

  useEffect(() => {
    if (open) onOpenRef.current?.()
  }, [open])

  useEffect(() => {
    if (!open || !desktop) return
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, desktop])

  const panel = (
    <div
      id={listId}
      className={cn(
        'flex flex-col bg-surface text-foreground',
        desktop
          ? 'absolute left-0 top-[calc(100%+4px)] z-30 max-h-80 w-72 overflow-hidden rounded-card border border-border shadow-lg'
          : 'max-h-[70vh]',
      )}
    >
      {searchable && (
        <div className="border-b border-border p-2">
          <label className="flex items-center gap-1.5 rounded-input border border-border bg-base px-2">
            <Search className="h-3.5 w-3.5 shrink-0 text-muted" />
            <input
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="搜索"
              className="h-8 w-full bg-transparent text-xs text-foreground outline-none placeholder:text-muted"
            />
          </label>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto p-1">
        {visible.length === 0 && (empty ?? <p className="px-2 py-3 text-xs text-muted">没有匹配项</p>)}
        {visible.map(item => {
          const on = selected.has(item.id)
          // 选不出股的事件不提供**新增**勾选 (勾了也是空炮)。
          const lacksMapping = item.selectableCount != null && item.selectableCount <= 0
          // 关键: 只要**已勾选**, 就必须保留勾选框形态与可点性 —— 否则用户取消不掉,
          // 而且「选中态」会在 count 归零后凭空消失(渲染与交互用了同一个条件时会这样)。
          // 这里刻意分成两个判断: `on` 决定**渲染成勾选框**, `blocked` 只决定**能否响应点击**。
          const showCheckbox = on || !lacksMapping
          const blocked = !on && lacksMapping
          const badge = selectableBadge(item)
          return (
            <button
              key={item.id}
              type="button"
              role="checkbox"
              aria-checked={on}
              aria-disabled={blocked || undefined}
              title={item.headline || undefined}
              onClick={() => { if (!blocked) onToggle(item.id) }}
              className={cn(
                'flex w-full items-start gap-2 rounded-btn px-2 py-1.5 text-left',
                blocked ? 'cursor-not-allowed opacity-60' : 'hover:bg-elevated',
              )}
            >
              {showCheckbox ? (
                <span className={cn(
                  'mt-0.5 grid h-3.5 w-3.5 shrink-0 place-items-center rounded border',
                  on ? 'border-accent bg-accent text-white' : 'border-border',
                  blocked && 'opacity-50',
                )}>
                  {on && <Check className="h-2.5 w-2.5" />}
                </span>
              ) : (
                <span className="mt-0.5 grid h-3.5 w-3.5 shrink-0 place-items-center text-muted" aria-hidden="true">
                  <Minus />
                </span>
              )}
              <span className="min-w-0">
                <span className="block whitespace-normal break-words text-xs text-foreground">
                  {item.name}
                  {(item.redCount ?? 0) > 0 && (
                    <span className="ml-1 rounded bg-bull/15 px-1 text-[10px] leading-4 text-bull">加红</span>
                  )}
                  {badge && (
                    <span className={cn('ml-1 rounded px-1 text-[10px] leading-4', badge.className)}>{badge.text}</span>
                  )}
                </span>
                {item.subtitle && (
                  <span className="mt-0.5 block text-[10px] leading-snug text-muted">{item.subtitle}</span>
                )}
                {(item.importance || item.category || item.direction) && (
                  <span className="mt-0.5 block text-[10px] leading-snug text-muted">
                    {[item.importance, item.confirmationLabel, item.category, item.direction, ...(item.mappedStocks ?? []).slice(0, 2)].filter(Boolean).join(' · ')}
                  </span>
                )}
                {item.concepts && item.concepts.length > 0 && (
                  <span className="mt-0.5 flex flex-wrap gap-1">
                    {item.concepts.map(tag => (
                      <span key={tag} className="rounded bg-accent/10 px-1 text-[10px] leading-4 text-accent">{tag}</span>
                    ))}
                  </span>
                )}
                {item.mentions != null ? (
                  <span className="mt-0.5 block text-[10px] leading-snug text-muted">
                    提及 {item.mentions} · 来源 {item.sourceCount ?? 0}
                    {item.updatedAt ? ` · 首见 ${item.updatedAt}` : ''}
                  </span>
                ) : item.description ? (
                  <span className="mt-0.5 block text-[10px] leading-snug text-muted">{item.description}</span>
                ) : null}
              </span>
            </button>
          )
        })}
      </div>
      {footer && <div className="border-t border-border p-2">{footer}</div>}
    </div>
  )

  return (
    <div ref={rootRef} className="relative min-w-0">
      <button
        type="button"
        disabled={disabled}
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen(value => !value)}
        className="flex h-9 w-full items-center gap-2 rounded-btn border border-border bg-surface px-2.5 text-left text-xs text-foreground hover:border-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span className="min-w-0 flex-1 truncate">{label}</span>
        {badge && (
          <span className="rounded bg-amber-500/15 px-1 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-500">
            {badge}
          </span>
        )}
        {selectedIds.length > 0 && (
          <span className="rounded-full bg-accent/15 px-1.5 text-[10px] font-medium text-accent">{selectedIds.length}</span>
        )}
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
      </button>
      {hint && <p className={cn('mt-1 text-[10px] leading-snug text-muted', hintClassName)}>{hint}</p>}
      {open && desktop && panel}
      {open && !desktop && (
        <div className="fixed inset-0 z-40 flex items-end bg-black/50" onClick={() => setOpen(false)}>
          <div
            className="w-full rounded-t-2xl border border-border bg-surface pb-[env(safe-area-inset-bottom)] shadow-xl"
            onClick={event => event.stopPropagation()}
          >
            <div className="flex items-center justify-between px-3 py-2">
              <span className="text-sm font-medium">{label}</span>
              <button type="button" className="text-xs text-accent" onClick={() => setOpen(false)}>完成</button>
            </div>
            {panel}
          </div>
        </div>
      )}
    </div>
  )
}
