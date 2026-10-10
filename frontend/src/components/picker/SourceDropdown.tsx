import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useIsDesktop } from '@/lib/useMediaQuery'

export interface SourceOption {
  id: string
  name: string
  description?: string
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
}

/** 带搜索的多选。桌面展开为下拉，窄屏展开为底部面板。 */
export function SourceDropdown({
  label, options, selectedIds, onToggle, badge, hint, empty, footer, disabled,
}: Props) {
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
      <div className="min-h-0 flex-1 overflow-y-auto p-1">
        {visible.length === 0 && (empty ?? <p className="px-2 py-3 text-xs text-muted">没有匹配项</p>)}
        {visible.map(item => {
          const on = selected.has(item.id)
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onToggle(item.id)}
              className="flex w-full items-start gap-2 rounded-btn px-2 py-1.5 text-left hover:bg-elevated"
            >
              <span className={cn(
                'mt-0.5 grid h-3.5 w-3.5 shrink-0 place-items-center rounded border',
                on ? 'border-accent bg-accent text-white' : 'border-border',
              )}>
                {on && <Check className="h-2.5 w-2.5" />}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-xs text-foreground">{item.name}</span>
                {item.description && (
                  <span className="mt-0.5 block text-[10px] leading-snug text-muted">{item.description}</span>
                )}
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
          <span className="rounded bg-amber-500/15 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-amber-500">
            {badge}
          </span>
        )}
        {selectedIds.length > 0 && (
          <span className="rounded-full bg-accent/15 px-1.5 text-[10px] font-medium text-accent">{selectedIds.length}</span>
        )}
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
      </button>
      {hint && <p className="mt-1 truncate text-[10px] text-muted">{hint}</p>}
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
