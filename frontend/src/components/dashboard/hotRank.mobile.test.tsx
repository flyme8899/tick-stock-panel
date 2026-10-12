// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AlertEvent, OverviewDimensionRankItem } from '@/lib/api'
import { HotRankCard, MonitorWidget } from './widgets'

const alerts = vi.hoisted(() => ([
  {
    ts: 1_700_000_000_000,
    source: 'signal',
    type: 'signal',
    symbol: '300123.SZ',
    name: '欢瑞世纪股份',
    message: '放量突破年线',
    price: 12.34,
    change_pct: 0.101,
    severity: 'info',
  },
  {
    ts: 1_700_000_100_000,
    source: 'price',
    type: 'price',
    symbol: '920001.BJ',
    name: '*ST某某股份',
    message: '跌破止损价',
    price: 3.21,
    change_pct: -0.0501,
    severity: 'warn',
  },
]) as AlertEvent[])

vi.mock('@/lib/api', () => ({
  api: {
    alertsList: async () => ({ alerts, total: alerts.length }),
    customSignalsList: async () => ({ signals: [] }),
  },
}))

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  // 手机约 390px，热度卡左右两列后单列大约 170px
  host.style.width = '170px'
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

const leading: OverviewDimensionRankItem[] = [{
  name: '半导体',
  count: 20,
  avg_pct: 0.0321,
  up_count: 18,
  down_count: 2,
  amount: 1,
  leader: { symbol: '300123.SZ', name: '欢瑞世纪股份', change_pct: 0.101 },
}]

const lagging: OverviewDimensionRankItem[] = [{
  name: '银行',
  count: 42,
  avg_pct: -0.021,
  up_count: 4,
  down_count: 38,
  amount: 1,
  leader: { symbol: '920001.BJ', name: '*ST某某股份', change_pct: -0.0501 },
}]

function rank(title: string) {
  return (
    <HotRankCard
      title={title}
      configUrl="/sector-analysis"
      rank={{ leading, lagging }}
    />
  )
}

it('keeps heat-card leader names at least ~4 CJK wide on a narrow column', async () => {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <div>
          {rank('概念热度')}
          {rank('行业热度')}
        </div>
      </MemoryRouter>,
    )
  })

  const names = [...host.querySelectorAll('[data-leader-name]')]
  expect(names).toHaveLength(4)
  for (const name of names) {
    expect(name.className).toContain('min-w-[4.5em]')
    expect(name.className).toContain('truncate')
    expect(name.className).toContain('flex-[1_1_4.5em]')
    const line = name.closest('[data-leader-line]')
    expect(line?.className).toContain('col-span-3')
    expect(line?.className).toContain('flex-wrap')
  }
  expect(host.textContent).toContain('欢瑞世纪股份')
  expect(host.textContent).toContain('*ST某某股份')

  const up = [...host.querySelectorAll('span')].find(el => el.textContent === '+10.10%')
  const down = [...host.querySelectorAll('span')].find(el => el.textContent === '-5.01%')
  expect(up?.className).toContain('text-bull')
  expect(down?.className).toContain('text-bear')
  expect(host.textContent).toContain('创')
  expect(host.textContent).toContain('北')
})

it('keeps monitor-widget stock names from collapsing beside price and percent', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <div style={{ width: 360 }}>
            <MonitorWidget onStockClick={() => {}} />
          </div>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0))
  })

  const names = [...host.querySelectorAll('[data-alert-name]')]
  expect(names.map(el => el.textContent)).toEqual(['欢瑞世纪股份', '*ST某某股份'])
  for (const name of names) {
    expect(name.className).toContain('min-w-[4.5em]')
    expect(name.className).toContain('truncate')
    const line = name.closest('[data-alert-line]')
    expect(line?.className).toContain('flex-wrap')
  }
  const up = [...host.querySelectorAll('span')].find(el => el.textContent === '+10.10%')
  const down = [...host.querySelectorAll('span')].find(el => el.textContent === '-5.01%')
  expect(up?.className).toContain('text-danger')
  expect(down?.className).toContain('text-bear')
  expect(host.textContent).toContain('创')
  expect(host.textContent).toContain('北')
})
