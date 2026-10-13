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

function hotRow(name: string): HTMLButtonElement {
  const found = [...host.querySelectorAll('button')].find(button => button.textContent?.includes(name))
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing hot row ${name}`)
  return found
}

async function toggleHot(name: string) {
  await act(async () => { hotRow(name).click() })
  await settle()
}

it('marks an exact selectable count and a capped one differently', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    { id: 'ev-1', name: '液冷服务器需求爆发', mentions: 6, source_count: 3,
      selectable_count: 12, mapping_breadth: 'usable', selectable_capped: false },
    // 命中 30 只 = 达到 _CONCEPT_CONSTITUENT_CAP, 是**上限**不是全集, 不能说「约」。
    { id: 'ev-2', name: '国企改革概念走强', mentions: 30, source_count: 9,
      selectable_count: 30, mapping_breadth: 'usable', selectable_capped: true },
  ]
  await renderPicker()
  await openMenu('热门事件')
  expect(host.textContent).toContain('12 只')
  expect(host.textContent).toContain('至少 30 只')
  expect(host.textContent).not.toContain('约 12 只')
  expect(host.textContent).not.toContain('约 30 只')
})

it('greys out an unmappable hot event and blocks selecting it', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    { id: 'ev-none', name: '美联储哈玛克讲话', mentions: 9, source_count: 5,
      selectable_count: 0, mapping_breadth: 'none',
      selectable_note: '「美联储哈玛克讲话」没有匹配到成分股' },
  ]
  await renderPicker()
  await openMenu('热门事件')
  // 原因来自后端 note, 不是千篇一律的「暂无对应个股」。
  expect(host.textContent).toContain('没有匹配到成分股')
  const row = hotRow('美联储哈玛克讲话')
  // 保留 checkbox 语义 + aria-checked, 且用 aria-disabled 保留可聚焦/可读屏。
  expect(row.getAttribute('role')).toBe('checkbox')
  expect(row.getAttribute('aria-checked')).toBe('false')
  expect(row.getAttribute('aria-disabled')).toBe('true')
  expect(row.disabled).toBe(false)
})

it('keeps showing a tick (not a minus) when a ticked item later drops to zero', async () => {
  // 关键场景: 勾上之后, 重算把这个事件的 selectable_count 归零。
  // 此时它仍然是「已勾选」状态, UI 必须继续显示已勾选, 而不是变成不可选的减号。
  // 用两次独立渲染来驱动"重算": 第一次勾选, 第二次数据里 count 已归零。
  harness.mode = 'ready'
  harness.hotItems = [
    { id: 'ev-flip', name: '先有映射的事件', mentions: 5, source_count: 2,
      selectable_count: 7, mapping_breadth: 'usable', selectable_capped: false },
  ]
  await renderPicker()
  await openMenu('热门事件')
  await toggleHot('先有映射的事件')
  expect(hotRow('先有映射的事件').querySelector('.lucide-check')).toBeTruthy()
  expect(hotRow('先有映射的事件').getAttribute('aria-checked')).toBe('true')

  // 重算: 同一个 id 的 selectable_count 归零, 列表重新取数并重新渲染。
  harness.hotItems = [
    { id: 'ev-flip', name: '先有映射的事件', mentions: 5, source_count: 2,
      selectable_count: 0, mapping_breadth: 'none',
      selectable_note: '「先有映射的事件」没有匹配到成分股' },
  ]
  await act(async () => { await client.invalidateQueries() })
  await settle()

  // 断言用户**实际看到的控件**: 必须仍是勾(Check), 不能是减号(Minus)。
  const row = hotRow('先有映射的事件')
  expect(row.querySelector('.lucide-check')).toBeTruthy()
  expect(row.querySelector('.lucide-minus')).toBeNull()
  expect(row.getAttribute('aria-checked')).toBe('true')
  // 且仍可取消。
  await toggleHot('先有映射的事件')
  expect(hotRow('先有映射的事件').getAttribute('aria-checked')).toBe('false')
})

it('marks a broad mapping as a wide range instead of a precise count', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    { id: 'ev-broad', name: 'AI概念全面开花', mentions: 20, source_count: 9,
      selectable_count: 30, mapping_breadth: 'broad', selectable_capped: true },
  ]
  await renderPicker()
  await openMenu('热门事件')
  expect(host.textContent).toContain('范围较大')
  expect(host.textContent).not.toContain('至少 30 只')
})

it('does not grey out a hot event when the backend omits the new fields', async () => {
  harness.mode = 'ready'
  harness.hotItems = [
    { id: 'ev-old', name: '旧快照事件', mentions: 4, source_count: 2 },
  ]
  await renderPicker()
  await openMenu('热门事件')
  const row = hotRow('旧快照事件')
  expect(row.getAttribute('aria-disabled')).toBeNull()
  expect(host.textContent).not.toContain('没有匹配到成分股')
})

it('keeps an unknown count selectable while dim index is not synced', async () => {
  // 维表未同步时后端给 selectable_count=null(未知), 不是 0(空炮)。
  // 此时实跑仍会纳入资讯提及个股, 所以**不能**置灰拦下勾选 —— 否则用户
  // 连本来能跑的事件都勾不上。原因要照常展示。
  harness.mode = 'ready'
  harness.hotItems = [
    { id: 'ev-pending', name: '机器人订单落地', mentions: 6, source_count: 3,
      selectable_count: null, mapping_breadth: null,
      selectable_note: '尚未同步同花顺行业/概念，本次只纳入资讯里提到的个股' },
  ]
  await renderPicker()
  await openMenu('热门事件')
  // 原因可见。
  expect(host.textContent).toContain('尚未同步同花顺行业/概念')
  const row = hotRow('机器人订单落地')
  // 未置灰, 可勾选。
  expect(row.getAttribute('aria-disabled')).toBeNull()
  await toggleHot('机器人订单落地')
  expect(hotRow('机器人订单落地').getAttribute('aria-checked')).toBe('true')
  // 未知数不能被渲染成「至少 N 只」这类量词 —— 那一行里不该出现「只」计数。
  // 注意别全局断言「只」: note 里「只纳入资讯里提到的个股」也含「只」。
  const text = hotRow('机器人订单落地').textContent ?? ''
  expect(text).not.toContain('至少')
  expect(text).not.toMatch(/\d+\s*只/)
})
