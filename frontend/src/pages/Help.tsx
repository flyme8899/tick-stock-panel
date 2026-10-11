import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { CircleHelp } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { HELP_TOPICS } from '@/help/guide'
import { cn } from '@/lib/cn'

function scrollToTopic(id: string) {
  document.getElementById(id)?.scrollIntoView({ block: 'start' })
}

export function Help() {
  const { hash } = useLocation()

  useEffect(() => {
    const id = hash.replace(/^#/, '')
    if (!id || !HELP_TOPICS.some(topic => topic.id === id)) return
    const frame = window.requestAnimationFrame(() => scrollToTopic(id))
    return () => window.cancelAnimationFrame(frame)
  }, [hash])

  return (
    <div className="flex min-h-full flex-col bg-base">
      <PageHeader
        title="使用说明"
        subtitle="按功能说明做什么、数据从哪来、多久更新、怎么用，以及容易看错的地方"
      />
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-4 sm:px-6">
        <p className="text-xs leading-5 text-muted">
          文中的数字是代码里的默认值。标成可配置的项会被环境变量或偏好覆盖，以当前部署为准。
        </p>
        <nav aria-label="说明目录" className="flex flex-wrap gap-1.5">
          {HELP_TOPICS.map(topic => (
            <a
              key={topic.id}
              href={`#${topic.id}`}
              onClick={event => {
                event.preventDefault()
                scrollToTopic(topic.id)
                window.history.replaceState(null, '', `#${topic.id}`)
              }}
              className="rounded-full border border-border bg-surface px-2.5 py-1 text-xs text-secondary transition-colors hover:border-accent/40 hover:text-foreground"
            >
              {topic.title}
            </a>
          ))}
        </nav>
        {HELP_TOPICS.map(topic => (
          <section
            key={topic.id}
            id={topic.id}
            className="scroll-mt-4 rounded-card border border-border bg-surface p-4 sm:p-5"
          >
            <h2 className="flex items-center gap-1.5 text-base font-semibold text-foreground">
              <CircleHelp className="h-4 w-4 shrink-0 text-accent" />
              {topic.title}
            </h2>
            <div className={cn('text-sm leading-6 text-secondary')}>
              <MarkdownRenderer content={topic.markdown} />
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}
