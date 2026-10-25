// @vitest-environment jsdom
/**
 * 两个插槽的降级行为（批次 20261025gdsa2 · 场景入口加深）。
 *
 * 要证的只有一件事：**服务没连上时，不许把人送进坏页面**。
 *
 * | status | stock-preview.footer | watchlist.toolbar |
 * |---|---|---|
 * | `reachable === true` | 链接「打开研报」→ `/dsa?section=reports&code=` | 可点按钮 |
 * | `reachable === false` | 灰字「决策服务未连接」，无链接 | `disabled` 按钮 |
 * | 未拿到结果 / 请求失败 | 灰字「检测中」，无链接 | `disabled` 按钮 |
 * | `enabled === true` 但 `reachable === false` | **走降级**（这是最要防的一种） |
 *
 * 零网络：`fetch` 被替换成固定 status。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import extension from './extension'

const slots = extension.slots as unknown as {
  name: string
  component: (props: Record<string, unknown>) => React.ReactElement
}[]

function slotFor(name: string) {
  const found = slots.find(item => item.name === name)
  if (!found) throw new Error(`扩展里没有注册插槽 ${name}`)
  return found.component
}

const StockReportLink = slotFor('stock-preview.footer')
const WatchlistDecision = slotFor('watchlist.toolbar')

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  client.clear()
  vi.unstubAllGlobals()
})

function stubStatus(payload: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })))
}

function stubFailure() {
  vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('boom') }))
}

async function render(node: React.ReactElement) {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter>{node}</MemoryRouter>
      </QueryClientProvider>,
    )
  })
  // 等 useQuery 落地并重渲染
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 30))
  })
}

function stockFooter() {
  return <StockReportLink symbol="600519" name="贵州茅台" view="daily" />
}

function watchlistToolbar() {
  return <WatchlistDecision symbols={['600519']} viewMode="table" selectedGroup="全部" refresh={() => {}} />
}

// ── reachable = true：正常链接 ────────────────────────────────

it('可达时个股预览页脚给「打开研报」链接，带 section=reports 与 code', async () => {
  stubStatus({ enabled: true, reachable: true, detail: '', base_url: 'http://x' })
  await render(stockFooter())
  const link = host.querySelector('a')
  expect(link).not.toBeNull()
  expect(link!.getAttribute('href')).toContain('/dsa?')
  expect(link!.getAttribute('href')).toContain('section=reports')
  expect(link!.getAttribute('href')).toContain('code=600519')
  expect(host.querySelector('[data-dsa-slot-reachable="true"]')).not.toBeNull()
})

it('可达时自选工具栏按钮可点（无 disabled）', async () => {
  stubStatus({ enabled: true, reachable: true, detail: '', base_url: 'http://x' })
  await render(watchlistToolbar())
  const button = host.querySelector('button')
  expect(button).not.toBeNull()
  expect(button!.hasAttribute('disabled')).toBe(false)
  expect(host.querySelector('[data-dsa-slot-reachable="true"]')).not.toBeNull()
})

// ── reachable = false：降级，不给链接 ────────────────────────

it('不可达时个股预览页脚降级成灰字，且没有 <a>（不把人送进坏页面）', async () => {
  stubStatus({ enabled: true, reachable: false, detail: 'connect ECONNREFUSED', base_url: 'http://x' })
  await render(stockFooter())
  expect(host.querySelector('a')).toBeNull()
  expect(host.textContent).toContain('决策服务未连接')
  expect(host.querySelector('[data-dsa-slot-degraded="offline"]')).not.toBeNull()
})

it('不可达时自选工具栏按钮 disabled，且文案说明未连接', async () => {
  stubStatus({ enabled: true, reachable: false, detail: '', base_url: 'http://x' })
  await render(watchlistToolbar())
  const button = host.querySelector('button')!
  expect(button.hasAttribute('disabled')).toBe(true)
  expect(button.textContent).toContain('未连接')
  expect(host.querySelector('[data-dsa-slot-degraded="offline"]')).not.toBeNull()
})

// ── enabled=true 但 reachable=false：最要防的那一种 ──────────

it('enabled=true 且 reachable=false → 两个插槽都走降级，绝不显示为可用', async () => {
  stubStatus({ enabled: true, reachable: false, detail: '', base_url: 'http://x' })

  await render(stockFooter())
  expect(host.querySelector('a')).toBeNull()
  expect(host.querySelector('[data-dsa-slot-reachable="true"]')).toBeNull()
  expect(host.textContent).toContain('决策服务未连接')

  await render(watchlistToolbar())
  expect(host.querySelector('button')!.hasAttribute('disabled')).toBe(true)
  expect(host.querySelector('[data-dsa-slot-reachable="true"]')).toBeNull()
})

// ── 未拿到结果 / 请求失败：检测中，不猜 ──────────────────────

it('reachable 字段缺失时按「检测中」降级，不冒充已连接', async () => {
  stubStatus({ enabled: true, detail: '', base_url: 'http://x' })
  await render(stockFooter())
  expect(host.querySelector('a')).toBeNull()
  expect(host.textContent).toContain('检测中')
  expect(host.querySelector('[data-dsa-slot-degraded="unknown"]')).not.toBeNull()
})

it('status 请求失败时同样降级（不抛、不阻塞宿主页面）', async () => {
  stubFailure()
  await render(stockFooter())
  expect(host.querySelector('a')).toBeNull()
  expect(host.textContent).toContain('检测中')
})

it('status 请求失败时自选按钮仍渲染（宿主页面不白屏）', async () => {
  stubFailure()
  await render(watchlistToolbar())
  const button = host.querySelector('button')
  expect(button).not.toBeNull()
  expect(button!.hasAttribute('disabled')).toBe(true)
})

// ── 共享缓存：不为一行提示多打一次 /status ───────────────────

it('两个插槽共用 dsa-status 缓存键', async () => {
  const fetchMock = vi.fn(async () => new Response(
    JSON.stringify({ enabled: true, reachable: true, detail: '', base_url: 'http://x' }),
    { status: 200, headers: { 'content-type': 'application/json' } },
  ))
  vi.stubGlobal('fetch', fetchMock)
  await render(stockFooter())
  expect(fetchMock).toHaveBeenCalledWith('/api/dsa/status')
  expect(client.getQueryData(['dsa-status'])).toBeTruthy()
})
