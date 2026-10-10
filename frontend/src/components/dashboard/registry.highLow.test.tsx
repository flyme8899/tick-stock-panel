// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it } from 'vitest'
import type { OverviewMarket } from '@/lib/api'
import type { DashboardItem } from './layout'
import { WIDGET_DEFS, type WidgetCtx } from './registry'

const item: DashboardItem = { i: 'trend', t: 'trend', x: 0, y: 0, w: 4, h: 8 }
let root: Root | undefined

afterEach(() => {
  act(() => root?.unmount())
  root = undefined
  document.body.innerHTML = ''
})

function renderWidget(id: string, data: Partial<OverviewMarket>) {
  const def = WIDGET_DEFS.find(widget => widget.id === id)
  if (!def) throw new Error(id)
  const host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  act(() => {
    root?.render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>{def.render({ data } as WidgetCtx, item)}</MemoryRouter>
      </QueryClientProvider>,
    )
  })
  return host.innerHTML
}

it('splits 新高/新低 into red up and green down', () => {
  const html = renderWidget('trend', {
    trend: {
      above_ma5: 0, above_ma20: 0, above_ma60: 0,
      above_ma5_pct: 55, above_ma20_pct: 39, above_ma60_pct: 48,
      new_high: 215, new_low: 226,
    },
    limit: {
      limit_up: 72, broken: 19, failed: 0, limit_down: 10, max_boards: 9, tiers: [],
    },
    activity: { avg_turnover: 1, high_turnover: 1022, high_vol_ratio: 27.6, vol_ratio: 1 },
  })
  expect(html).toContain('<span class="text-bull">215</span><span class="text-muted">/</span><span class="text-bear">226</span>')
  expect(html).not.toContain('215/226')
})

it('keeps other overview up/down pairs on separate colors', () => {
  const html = renderWidget('kpi', {
    breadth: {
      total: 5000, up: 1800, down: 2900, flat: 300, up_pct: 36, down_pct: 58,
      strong_up: 400, strong_down: 700,
    },
    limit: {
      limit_up: 72, broken: 19, failed: 0, limit_down: 10, max_boards: 9,
      seal_rate: 79, tiers: [],
    },
    amount: { total: 1e12, avg: 2e8 },
    activity: { avg_turnover: 2.4, high_turnover: 1022, high_vol_ratio: 27.6, vol_ratio: 1.2 },
  })
  expect(html).toContain('<span class="text-bull">1800</span>')
  expect(html).toContain('<span class="text-bear">2900</span>')
  expect(html).toContain('<span class="text-bull">400</span><span class="text-muted">/</span><span class="text-bear">700</span>')
  expect(html).toContain('<span class="text-bull">72</span><span class="text-muted">/</span><span class="text-bear">10</span>')
})
