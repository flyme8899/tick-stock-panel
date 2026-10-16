// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FundFlowBoard } from '@/lib/api'
import { FundFlow } from './FundFlow'

const board: FundFlowBoard = {
  industry: { trade_date: null, snapshot: null, items: [] },
  concept: { trade_date: null, snapshot: null, items: [] },
  stocks_today: { trade_date: null, items: [] },
  stocks_5d: { trade_date: null, items: [] },
  margin: { items: [] },
  southbound: { items: [] },
  etf_shares: {
    trade_date: '2026-10-14',
    prev_trade_date: '2026-10-13',
    baselines: { '1': '2026-10-13', '5': null, '15': null },
    items: [
      {
        code: '510300',
        name: '沪深300ETF',
        shares: 12e8,
        change_1d: 2e8,
        change_5d: null,
        change_15d: null,
        broad: '沪深300',
        net_inflow_1d: 10,
        net_inflow_5d: 15.5,
        net_inflow_20d: null,
        flow_net: 13,
        flow_net_5d: 4,
        flow_net_20d: 8,
        flow_diverged: true,
      },
      {
        code: '588000',
        name: '科创50ETF',
        shares: 3e8,
        change_1d: 1e8,
        net_inflow_1d: 2,
        net_inflow_5d: null,
        net_inflow_20d: null,
        flow_net: 2.1,
        flow_diverged: false,
      },
    ],
  },
}

vi.mock('@/lib/api', () => ({
  api: {
    fundFlowBoard: async () => board,
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
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
  })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

it('shows computed inflow beside share changes and flags a diverged navigator row', async () => {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <FundFlow />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await settle()

  const text = host.textContent || ''
  expect(text).toContain('+2.00亿份')
  expect(text).toContain('净流入')
  expect(text).toContain('1日 +10.00亿元')
  expect(text).toContain('5日 +15.50亿元')
  expect(text).toContain('20日 —')
  expect(text).toContain('领航者净流入 +13.00亿元')
  expect(text).toContain('5日 +4.00亿元')
  expect(text).toContain('20日 +8.00亿元')
  expect(text).toContain('与领航者相差超过 20%')
  expect(text).toContain('领航者净流入 +2.10亿元')

  const rows = [...host.querySelectorAll('li')]
  const diverged = rows.find(row => row.textContent?.includes('510300'))
  const aligned = rows.find(row => row.textContent?.includes('588000'))
  expect(diverged?.className).toContain('border-warning/40')
  expect(diverged?.getAttribute('title')).toBe('推算净流入与领航者相差超过 20%')
  expect(aligned?.className).not.toContain('border-warning')
  expect(aligned?.textContent).not.toContain('相差超过 20%')
})
