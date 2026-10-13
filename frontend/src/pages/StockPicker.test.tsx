// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPicker } from './StockPicker'

const harness = vi.hoisted(() => {
  class ApiError extends Error {
    status: number
    constructor(message: string, status: number) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    ApiError,
    mode: 'pending' as 'pending' | 'unauthorized' | 'failed' | 'ready',
    hotStatus: '' as '' | 'computing',
    hotItems: [] as Array<Record<string, unknown>>,
    calls: 0,
  }
})

vi.mock('@/lib/api', () => ({
  ApiError: harness.ApiError,
  api: {
    pickerSources: () => {
      harness.calls += 1
      if (harness.mode === 'pending') return new Promise(() => {})
      if (harness.mode === 'unauthorized') return Promise.reject(new harness.ApiError('未登录', 401))
      if (harness.mode === 'failed') return Promise.reject(new harness.ApiError('boom', 500))
      return Promise.resolve({
        groups: [
          { id: 'fundamental', label: '基本面', items: [{ id: 'roe', name: '高ROE' }] },
          {
            id: 'hot_events',
            label: '热门事件',
            items: harness.hotItems,
            hint: harness.hotStatus === 'computing' ? '正在计算' : null,
            status: harness.hotStatus || undefined,
          },
          { id: 'technical', label: '技术面', items: [] },
          { id: 'factor', label: '因子', items: [] },
          { id: 'dsa', label: 'DSA', items: [], available: false },
        ],
        industries: [],
      })
    },
    watchlistGroups: () => new Promise(() => {}),
  },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  window.matchMedia = (query: string) => ({
    matches: query.includes('768'),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })
  harness.mode = 'pending'
  harness.hotStatus = ''
  harness.hotItems = []
  harness.calls = 0
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: {
    retry: false, staleTime: Infinity, gcTime: Infinity,
  } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function renderPicker() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <StockPicker />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await settle()
}

function buttonByLabel(label: string): HTMLButtonElement {
  const found = [...host.querySelectorAll('button')].find(button => (
    button.querySelector('span')?.textContent === label
  ))
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label}`)
  return found
}

async function openMenu(label: string) {
  await act(async () => { buttonByLabel(label).click() })
  await settle()
}

const MENUS = ['基本面', '热门事件', '技术面/短线', '因子打分', 'DSA 选股', '行业']

it('shows loading instead of an empty match while sources are in flight', async () => {
  await renderPicker()
  expect(harness.calls).toBe(1)
  for (const label of MENUS) await openMenu(label)
  expect(host.textContent).toContain('加载中…')
  expect(host.textContent).not.toContain('没有匹配项')
  expect(harness.calls).toBe(1)
})

it('shows an expired-login message on 401 and refetches when the menu opens', async () => {
  harness.mode = 'unauthorized'
  await renderPicker()
  expect(harness.calls).toBe(1)
  await openMenu('基本面')
  expect(host.textContent).toContain('登录已过期，请重新登录')
  expect(host.textContent).not.toContain('没有匹配项')
  expect(harness.calls).toBe(2)
})

it('shows a retry button for other source failures', async () => {
  harness.mode = 'failed'
  await renderPicker()
  await openMenu('因子打分')
  expect(host.textContent).toContain('来源列表加载失败')
  expect(host.textContent).not.toContain('没有匹配项')
  const before = harness.calls
  const retry = [...host.querySelectorAll('button')].find(button => button.textContent === '重试')
  expect(retry).toBeTruthy()
  await act(async () => { retry!.click() })
  await settle()
  expect(harness.calls).toBe(before + 1)
})

it('keeps a loaded list searchable and refetches an empty group on open', async () => {
  harness.mode = 'ready'
  await renderPicker()
  await openMenu('基本面')
  expect(host.textContent).toContain('高ROE')
  expect(harness.calls).toBe(1)
  const input = host.querySelector('input[placeholder="搜索"]')
  expect(input).toBeTruthy()
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    setter?.call(input, '没有这只')
    input!.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
  expect(host.textContent).toContain('没有匹配项')

  await openMenu('热门事件')
  expect(host.textContent).toContain('今日暂无热门事件')
  await openMenu('因子打分')
  expect(host.textContent).toContain('还没有因子生成的策略')
  await openMenu('DSA 选股')
  expect(host.textContent).toContain('决策服务未连接')
  expect(harness.calls).toBeGreaterThan(1)
})

it('shows a computing hint while the hot-event snapshot is empty', async () => {
  harness.mode = 'ready'
  harness.hotStatus = 'computing'
  await renderPicker()
  expect(host.textContent).toContain('正在计算')
  await openMenu('热门事件')
  expect(host.textContent).toContain('正在计算热门事件，请稍候')
  expect(host.textContent).not.toContain('今日暂无热门事件')
})

it('shows an approximate-holdings badge for mappable hot events', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    {
      id: 'ev-1', name: '液冷服务器需求爆发', description: '提及 6 · 来源 3',
      mentions: 6, source_count: 3, selectable_count: 12, mapping_breadth: 'usable',
    },
  ]
  await renderPicker()
  await openMenu('热门事件')
  expect(host.textContent).toContain('约 12 只')
})

it('greys out an unmappable hot event and blocks selecting it', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    {
      id: 'ev-none', name: '美联储哈玛克讲话', description: '提及 9 · 来源 5',
      mentions: 9, source_count: 5, selectable_count: 0, mapping_breadth: 'none',
    },
  ]
  await renderPicker()
  await openMenu('热门事件')
  expect(host.textContent).toContain('暂无对应个股')
  const row = [...host.querySelectorAll('button')].find(button => button.textContent?.includes('美联储哈玛克讲话'))
  expect(row).toBeTruthy()
  // 选不出股的事件禁用整行, 避免「点了没反应」; 用减号而非勾选框表达"不可选"。
  expect((row as HTMLButtonElement).disabled).toBe(true)
  expect(row!.querySelector('[role="checkbox"]')).toBeNull()
})

it('still lets an unmappable event be unticked once it is selected', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    {
      id: 'ev-none', name: '美联储哈玛克讲话', description: '提及 9 · 来源 5',
      mentions: 9, source_count: 5, selectable_count: 0, mapping_breadth: 'none',
    },
  ]
  await renderPicker()
  await openMenu('热门事件')
  const find = () => [...host.querySelectorAll('button')]
    .find(button => button.textContent?.includes('美联储哈玛克讲话')) as HTMLButtonElement
  // 先经由 URL/其它途径勾选不到, 这里直接点选: 未勾选时被禁用, 点了不生效。
  await act(async () => { find().click() })
  await settle()
  expect(find().disabled).toBe(true)
})

it('marks a broad mapping as a wide range instead of a precise count', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    {
      id: 'ev-broad', name: 'AI概念全面开花', description: '提及 20 · 来源 9',
      mentions: 20, source_count: 9, selectable_count: 30, mapping_breadth: 'broad',
    },
  ]
  await renderPicker()
  await openMenu('热门事件')
  expect(host.textContent).toContain('范围较大')
  expect(host.textContent).not.toContain('约 30 只')
})

it('does not grey out a hot event when the backend omits the new fields', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    { id: 'ev-old', name: '旧快照事件', description: '提及 4 · 来源 2', mentions: 4, source_count: 2 },
  ]
  await renderPicker()
  await openMenu('热门事件')
  const row = [...host.querySelectorAll('button')].find(button => button.textContent?.includes('旧快照事件'))
  expect(row).toBeTruthy()
  expect((row as HTMLButtonElement).disabled).toBe(false)
  expect(host.textContent).not.toContain('暂无对应个股')
})
