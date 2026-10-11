import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { CircleHelp } from 'lucide-react'
import { HELP_PATH, helpTopic } from '@/help/guide'
import { cn } from '@/lib/cn'

interface Props {
  topicId: string
  /** 问号靠近右缘时，气泡向左对齐，避免溢出屏幕 */
  align?: 'start' | 'end'
  className?: string
}

export function PageHelpButton({ topicId, align = 'start', className }: Props) {
  const topic = helpTopic(topicId)
  const [open, setOpen] = useState(false)
  const [box, setBox] = useState<{ top: number; left: number; width: number } | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const panelId = useId()

  useEffect(() => {
    if (!open) return
    const place = () => {
      const button = rootRef.current?.querySelector('button')
      if (!button) return
      const rect = button.getBoundingClientRect()
      const width = Math.min(320, window.innerWidth - 16)
      const preferred = align === 'end' ? rect.right - width : rect.left
      const left = Math.max(8, Math.min(preferred, window.innerWidth - width - 8))
      const below = rect.bottom + 6
      const panelHeight = panelRef.current?.offsetHeight ?? 140
      const top = below + panelHeight > window.innerHeight - 8 && rect.top > panelHeight + 8
        ? Math.max(8, rect.top - panelHeight - 6)
        : below
      setBox({ top, left, width })
    }
    place()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node
      if (rootRef.current?.contains(target) || panelRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointer)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointer)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open, align])

  if (!topic) return null

  return (
    <div ref={rootRef} className={cn('relative shrink-0', className)}>
      <button
        type="button"
        aria-label={`${topic.title}使用说明`}
        aria-expanded={open}
        aria-controls={panelId}
        title="使用说明"
        onClick={() => setOpen(value => !value)}
        className="inline-flex h-6 w-6 items-center justify-center rounded-full text-muted transition-colors hover:bg-elevated hover:text-foreground"
      >
        <CircleHelp className="h-3.5 w-3.5" />
      </button>
      {open && createPortal(
        <div
          ref={panelRef}
          id={panelId}
          role="dialog"
          aria-label={topic.title}
          style={box ? { top: box.top, left: box.left, width: box.width } : { top: 8, left: 8, width: Math.min(320, window.innerWidth - 16) }}
          className="fixed z-[80] rounded-card border border-border bg-surface p-3 text-left text-xs leading-5 text-secondary shadow-lg"
        >
          <p className="font-medium text-foreground">{topic.title}</p>
          <p className="mt-1 max-h-48 overflow-y-auto">{topic.summary}</p>
          <Link
            to={`${HELP_PATH}#${topic.id}`}
            className="mt-2 inline-flex text-accent hover:underline"
            onClick={() => setOpen(false)}
          >
            查看完整说明
          </Link>
        </div>,
        document.body,
      )}
    </div>
  )
}
