// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FundFlowAmountRow, FundFlowBoard } from '@/lib/api'
import { ETF_GROUP_COLLAPSE_KEY, FundFlow } from './FundFlow'

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
  localStorage.clear()
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

async function renderPage() {
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
}

function groupButton(label: string): HTMLButtonElement {
  const node = host.querySelector(`[data-etf-group="${label}"]`)
  if (!(node instanceof HTMLButtonElement)) throw new Error(`missing group ${label}`)
  return node
}

async function clickGroup(label: string) {
  await act(async () => {
    groupButton(label).click()
  })
}

function rowByCode(code: string): HTMLElement | undefined {
  return [...host.querySelectorAll('li')].find(row => row.textContent?.includes(code))
}

// 折叠态的三列合计。按 data-window 取，避免依赖节点顺序或整段 textContent。
function inflowCell(label: string, window: '1d' | '5d' | '20d'): HTMLElement | null {
  return groupButton(label).querySelector(`[data-etf-group-inflow][data-window="${window}"]`)
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
  expect(text).toContain('基准份额乘上其后的折算倍数')
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

it('collapses one ETF group from its title and keeps the others open', async () => {
  const original = board.etf_shares.items
  board.etf_shares.items = [
    ...original,
    {
      code: '510310',
      name: '沪深300ETF易方达',
      broad: '沪深300',
      shares: 4e8,
      change_1d: 1e8,
      net_inflow_1d: 6.31,
      net_inflow_5d: -2.2,
      net_inflow_20d: 7.7,
    },
    {
      code: '159915',
      name: '创业板ETF',
      broad: '创业板',
      shares: 2e8,
      change_1d: -1e8,
      net_inflow_1d: -1.5,
      // 创业板组只有这一只，三个窗口都有数
      net_inflow_5d: -3.5,
      net_inflow_20d: null,
    },
    {
      code: '512100',
      name: '中证1000ETF',
      shares: 1e8,
      change_1d: 0.5e8,
      net_inflow_1d: 0.4,
    },
  ]
  try {
    await renderPage()

    for (const label of ['沪深300', '科创50', '创业板', '其他']) {
      const header = groupButton(label)
      expect(header.getAttribute('aria-expanded')).toBe('true')
      expect(header.getAttribute('data-expanded')).toBe('true')
      expect(header.querySelector('svg')?.getAttribute('class')).toContain('rotate-90')
      expect(header.querySelector('[data-etf-group-inflow]')).toBeNull()
    }
    expect(groupButton('沪深300').querySelector('[data-etf-group-count]')?.textContent).toBe('2 只')
    expect(groupButton('创业板').textContent).not.toContain('国家队代理')
    expect(groupButton('其他').querySelector('[data-etf-group-count]')?.textContent).toBe('1 只')
    expect(rowByCode('510300')).toBeTruthy()
    expect(rowByCode('159915')).toBeTruthy()

    await clickGroup('沪深300')

    const collapsed = groupButton('沪深300')
    expect(collapsed.getAttribute('aria-expanded')).toBe('false')
    expect(collapsed.querySelector('svg')?.getAttribute('class')).not.toContain('rotate-90')
    expect(collapsed.textContent).toContain('沪深300')
    expect(collapsed.textContent).toContain('2 只')
    expect(collapsed.textContent).toContain('国家队代理')
    // 三个窗口各自合计，列内不再写「N日合计」前缀
    const d1 = inflowCell('沪深300', '1d')
    expect(d1?.textContent).toBe('+16.31亿元')
    expect(d1?.className).toContain('text-danger')
    // 15.5 + (-2.2)
    expect(inflowCell('沪深300', '5d')?.textContent).toBe('+13.30亿元')
    expect(inflowCell('沪深300', '5d')?.className).toContain('text-danger')
    // 510300 的 20d 为 null，整列留空
    expect(inflowCell('沪深300', '20d')?.textContent).toBe('')
    expect(inflowCell('沪深300', '20d')?.className).not.toContain('text-danger')
    expect(inflowCell('沪深300', '20d')?.className).not.toContain('text-bear')
    expect(collapsed.textContent).not.toContain('合计')

    const windows = host.querySelector('[data-etf-windows]')
    const summaryColumns = d1?.parentElement
    expect(windows?.children).toHaveLength(3)
    expect(windows?.children[0]?.textContent).toBe('1日')
    expect(windows?.children[1]?.textContent).toBe('5日')
    expect(windows?.children[2]?.textContent).toBe('20日')
    expect(summaryColumns?.children).toHaveLength(3)
    expect(summaryColumns?.children[0]).toBe(d1)
    for (const index of [0, 1, 2]) {
      expect(summaryColumns?.children[index]?.className).toContain('w-[5.75rem]')
      expect(windows?.children[index]?.className).toContain('w-[5.75rem]')
    }
    expect(rowByCode('510300')).toBeUndefined()
    expect(rowByCode('510310')).toBeUndefined()
    expect(rowByCode('588000')?.textContent).toContain('-2.00亿元')
    expect(rowByCode('159915')).toBeTruthy()
    expect(rowByCode('512100')).toBeTruthy()
    expect(JSON.parse(localStorage.getItem(ETF_GROUP_COLLAPSE_KEY) || '[]')).toEqual(['沪深300'])

    await clickGroup('创业板')
    expect(inflowCell('创业板', '1d')?.textContent).toBe('-1.50亿元')
    expect(inflowCell('创业板', '1d')?.className).toContain('text-bear')
    expect(inflowCell('创业板', '5d')?.textContent).toBe('-3.50亿元')
    expect(inflowCell('创业板', '5d')?.className).toContain('text-bear')
    expect(inflowCell('创业板', '20d')?.textContent).toBe('')
    expect(rowByCode('159915')).toBeUndefined()
    expect(JSON.parse(localStorage.getItem(ETF_GROUP_COLLAPSE_KEY) || '[]')).toEqual(['沪深300', '创业板'])
  } finally {
    board.etf_shares.items = original
  }
})

it('restores collapsed groups from localStorage and clears the record when reopened', async () => {
  localStorage.setItem(ETF_GROUP_COLLAPSE_KEY, JSON.stringify(['科创50']))
  await renderPage()

  expect(groupButton('科创50').getAttribute('aria-expanded')).toBe('false')
  // 科创50 只有 510300 以外的 588000：1d 有数，5d / 20d 为 null，对应列留空
  expect(inflowCell('科创50', '1d')?.textContent).toBe('-2.00亿元')
  expect(inflowCell('科创50', '1d')?.className).toContain('text-bear')
  expect(inflowCell('科创50', '5d')?.textContent).toBe('')
  expect(inflowCell('科创50', '20d')?.textContent).toBe('')
  expect(groupButton('科创50').textContent).not.toContain('合计')
  expect(rowByCode('588000')).toBeUndefined()
  expect(groupButton('沪深300').getAttribute('aria-expanded')).toBe('true')
  expect(rowByCode('510300')).toBeTruthy()

  await clickGroup('科创50')
  expect(rowByCode('588000')).toBeTruthy()
  expect(localStorage.getItem(ETF_GROUP_COLLAPSE_KEY)).toBeNull()

  await act(async () => root.unmount())
  root = createRoot(host)
  await renderPage()
  expect(groupButton('科创50').getAttribute('aria-expanded')).toBe('true')
  expect(rowByCode('588000')).toBeTruthy()
})

it('does not invent a 1-day total when a row in the group has no inflow', async () => {
  const original = board.etf_shares.items
  const gap: FundFlowAmountRow = {
    code: '510330',
    name: '沪深300ETF华夏',
    broad: '沪深300',
    shares: 8e8,
    change_1d: 0.2e8,
    net_inflow_1d: null,
    // 5d / 20d 有数，验证缺口只影响 1d 那一列
    net_inflow_5d: 3.3,
    net_inflow_20d: 4.4,
  }
  board.etf_shares.items = [...original, gap]
  try {
    await renderPage()
    await clickGroup('沪深300')
    const header = groupButton('沪深300')
    expect(header.querySelector('[data-etf-group-count]')?.textContent).toBe('2 只')
    // 1d 组内有一只缺数 → 这一列空
    expect(inflowCell('沪深300', '1d')?.textContent).toBe('')
    // 5d：510300 的 15.5 + 510330 的 3.3；20d：510300 为 null → 整列空
    expect(inflowCell('沪深300', '5d')?.textContent).toBe('+18.80亿元')
    expect(inflowCell('沪深300', '20d')?.textContent).toBe('')
    expect(header.textContent).not.toContain('合计')
    expect(rowByCode('510300')).toBeUndefined()
    expect(rowByCode('510330')).toBeUndefined()
  } finally {
    board.etf_shares.items = original
  }
})

it('hides the whole inflow block when every window of the group is missing', async () => {
  const original = board.etf_shares.items
  const base = original[0]!
  board.etf_shares.items = [
    { ...base, net_inflow_1d: null, net_inflow_5d: null, net_inflow_20d: null },
  ]
  try {
    await renderPage()
    await clickGroup('沪深300')
    const header = groupButton('沪深300')
    expect(header.getAttribute('aria-expanded')).toBe('false')
    // 三窗口皆缺：整块不渲染，不留三个空列误导
    expect(header.querySelector('[data-etf-group-inflow]')).toBeNull()
    expect(header.textContent).not.toContain('合计')
    expect(rowByCode('510300')).toBeUndefined()
  } finally {
    board.etf_shares.items = original
  }
})

it('ignores a corrupt collapse record and leaves groups expanded', async () => {
  localStorage.setItem(ETF_GROUP_COLLAPSE_KEY, '{')
  await renderPage()
  expect(groupButton('沪深300').getAttribute('aria-expanded')).toBe('true')
  expect(groupButton('科创50').getAttribute('aria-expanded')).toBe('true')
  expect(rowByCode('510300')).toBeTruthy()
  expect(rowByCode('588000')).toBeTruthy()
})
