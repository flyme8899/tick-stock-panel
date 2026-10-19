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
    baselines: { '1': '2026-10-13', '5': '2026-10-09', '20': null },
    items: [
      {
        code: '510300',
        name: '沪深300ETF',
        shares: 12e8,
        change_1d: 2e8,
        change_5d: -3e8,
        change_20d: null,
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
        broad: '科创50',
        shares: 3e8,
        change_1d: 1e8,
        net_inflow_1d: -2,
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

it('aligns 1/5/20 share changes with net inflow and flags a diverged navigator row', async () => {
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
  expect(text).toContain('5 日 / 20 日是相邻交易日的滚动合计')
  expect(text).not.toContain('15日')
  const headers = host.querySelector('[data-etf-windows]')
  expect(headers?.textContent?.replace(/\s+/g, '')).toBe('1日5日20日')

  const rows = [...host.querySelectorAll('li')]
  const diverged = rows.find(row => row.textContent?.includes('510300'))
  const aligned = rows.find(row => row.textContent?.includes('588000'))
  const columns = [...(diverged?.querySelectorAll('[data-window]') ?? [])]
  expect(columns.map(node => node.getAttribute('data-window'))).toEqual(['1', '5', '20'])
  const [day1, day5, day20] = columns
  if (!day1 || !day5 || !day20) throw new Error('missing window columns')

  const shareOf = (column: Element) => column.querySelector('[data-kind="shares"]')
  const inflowOf = (column: Element) => column.querySelector('[data-kind="inflow"]')
  expect(shareOf(day1)?.textContent).toBe('+2.00亿份')
  expect(inflowOf(day1)?.textContent).toBe('+10.00亿元')
  expect(shareOf(day1)?.className).toContain('text-danger')
  expect(inflowOf(day1)?.className).toContain('text-danger')
  expect(shareOf(day1)?.getAttribute('title')).toBe('较 2026-10-13（前 1 份落盘）')
  expect(shareOf(day5)?.textContent).toBe('-3.00亿份')
  expect(shareOf(day5)?.className).toContain('text-bear')
  expect(inflowOf(day5)?.textContent).toBe('+15.50亿元')
  expect(shareOf(day5)?.getAttribute('title')).toBe('较 2026-10-09（前 5 份落盘）')
  expect(shareOf(day20)?.textContent).toBe('—')
  expect(inflowOf(day20)?.textContent).toBe('—')
  expect(shareOf(day20)?.className).toContain('text-muted')
  expect(shareOf(day20)?.getAttribute('title')).toBe('不足 20 份落盘，无法计算')

  const alignedInflow = aligned?.querySelector('[data-window="1"] [data-kind="inflow"]')
  expect(alignedInflow?.textContent).toBe('-2.00亿元')
  expect(alignedInflow?.className).toContain('text-bear')

  expect(text).toContain('领航者净流入 +13.00亿元')
  expect(text).toContain('5日 +4.00亿元')
  expect(text).toContain('20日 +8.00亿元')
  expect(text).toContain('与领航者相差超过 20%')
  expect(text).toContain('领航者净流入 +2.10亿元')
  expect(diverged?.className).toContain('border-warning/40')
  expect(diverged?.getAttribute('title')).toBe('推算净流入与领航者相差超过 20%')
  expect(aligned?.className).not.toContain('border-warning')
  expect(aligned?.textContent).not.toContain('相差超过 20%')

  const nameNode = diverged?.querySelector('[data-etf-name]')
  expect(nameNode?.getAttribute('title')).toBe('沪深300ETF')
  expect(nameNode?.querySelector('[data-etf-code]')?.textContent).toBe('510300')
  expect(nameNode?.querySelector('[data-etf-code]')?.className).toContain('text-muted')
  expect(host.querySelector('[data-etf-group="沪深300"]')?.textContent).toContain('国家队代理')
  expect(host.querySelector('[data-etf-group="科创50"]')?.textContent).toContain('国家队代理')
  expect(host.querySelector('[data-etf-list]')?.className).toContain('max-h-80')
  expect(host.querySelector('[data-etf-list]')?.className).toContain('overflow-y-auto')
})
