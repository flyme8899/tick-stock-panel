// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AbnormalMoves } from './AbnormalMoves'

vi.mock('@/lib/api', () => ({
  api: {
    quoteStatus: async () => ({ is_trading_hours: false }),
    abnormalIntraday: async () => ({
      cache_date: '2026-10-12',
      counts: { limit_up: 1 },
      rows: [{
        symbol: '000999.SZ',
        name: '华润三九',
        close: 42.5,
        change_pct: 0.021,
        amplitude: 0.03,
        vol_ratio_5d: 1.2,
        turnover_rate: 3.4,
        consecutive_limit_ups: 0,
        signals: ['limit_up'],
      }],
    }),
    auctionBenchmark: async () => ({
      state: 'ok',
      trade_date: '2026-10-12',
      count: 2,
      items: [
        {
          thscode: '600825.SH',
          ticker: '600825',
          name: '新华传媒',
          auction_pct: 9.98,
          tags: ['新股', '出局', '高送转'],
          day0_oc: null,
          day0_pct: null,
          d1_pct: null,
        },
        {
          thscode: '002242.SZ',
          ticker: '002242',
          name: '九阳股份',
          auction_pct: 2.15,
          tags: [],
          day0_oc: 0.012,
          day0_pct: 0.02,
          d1_pct: null,
        },
      ],
    }),
    abnormalOverview: async () => { throw new Error('偏移计算不应在未开启时请求') },
    watchlistList: async () => ({ symbols: [] }),
  },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  for (let i = 0; i < 8; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function renderPage() {
  await act(async () => root.render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <AbnormalMoves />
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await settle()
}

it('shows the full intraday name and leaves room for the mobile menu button', async () => {
  await renderPage()
  const name = host.querySelector('[data-stock-name]')
  expect(name?.textContent).toBe('华润三九')
  expect(name?.className).not.toContain('truncate')
  expect(name?.className).not.toContain('max-w-40')
  expect(host.querySelector('.max-md\\:\\!pl-14')).not.toBeNull()
  expect(host.textContent).toContain('涨停')
  expect(host.textContent).toContain('+2.10%')
})

it('auction list keeps the full name, full tags, and an em dash before the open', async () => {
  await renderPage()
  const tab = [...host.querySelectorAll('button')].find(el => el.textContent?.includes('竞价异动'))
  expect(tab).toBeTruthy()
  await act(async () => tab!.click())
  await settle()

  expect(host.textContent).toContain('盘前 9:15-9:25')
  const desc = [...host.querySelectorAll('p')].find(el => el.textContent?.includes('盘前 9:15-9:25'))
  expect(desc?.className.split(/\s+/)).not.toContain('truncate')

  const names = [...host.querySelectorAll('[data-stock-name]')].map(el => el.textContent)
  expect(names).toEqual(['新华传媒', '九阳股份'])
  for (const el of host.querySelectorAll('[data-stock-name]')) {
    expect(el.className).not.toContain('truncate')
  }
  expect(host.textContent).toContain('600825')
  expect(host.textContent).toContain('002242')
  expect(host.textContent).toContain('追高风险')
  expect(host.textContent).toContain('新股')
  expect(host.textContent).toContain('出局')
  expect(host.textContent).toContain('高送转')
  expect(host.textContent).toContain('当日')
  expect(host.textContent).toContain('—')
  expect(host.textContent).toContain('+1.20%')
  const chase = [...host.querySelectorAll('span')].find(el => el.textContent === '追高风险')
  const tag = [...host.querySelectorAll('span')].find(el => el.textContent === '新股')
  expect(chase?.className).toContain('rounded')
  expect(chase?.className).toContain('border')
  expect(tag?.className).toContain('rounded')
  expect(tag?.className).toContain('border')
  expect(tag?.className).not.toContain('truncate')
})
