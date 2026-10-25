// @vitest-environment jsdom
/**
 * L4 三个动作的**成功路径**真的会 bump（批次 20261025gdsa2 ④）。
 *
 * 任务书验收 ④-1 要求「三 action 成功路径均可测到 core_action_count +1」。
 * 这里走真实组件 + 真实 mutation，只把 `fetch` 换掉：
 *
 * - Reports「生成研报」→ `report_submit`
 * - Reports「登记狙击线」→ `sniper_register`（判据 `replaced || lines.length>0`）
 * - 问股「发送」→ `chat_send`
 *
 * 另外钉一条反向的：**失败不许 bump**。`replaced:false` 且 `lines:[]` 时
 * 计数必须纹丝不动 —— 否则「点了个没生效的按钮」也会被记成留存动作。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Chat, Reports } from './DecisionWorkspace'
import {
  DSA_FUNNEL_KEY_CORE_ACTION_COUNT,
  readActionCount,
  readCoreActionCount,
  resetFunnelCounts,
} from './funnelMetrics'

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  resetFunnelCounts()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 }, mutations: { retry: false } },
  })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  resetFunnelCounts()
  localStorage.clear()
  vi.unstubAllGlobals()
})

/** 按 URL 路由的 fetch 桩。返回请求记录，便于断言打到哪。 */
function stubRoutes(routes: Record<string, { status?: number; body: unknown }>) {
  const calls: { url: string; init?: RequestInit }[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init })
    const hit = routes[url]
    if (!hit) throw new Error(`未桩住的请求: ${url}`)
    return new Response(JSON.stringify(hit.body), {
      status: hit.status ?? 200,
      headers: { 'content-type': 'application/json' },
    })
  }))
  return calls
}

async function render(node: React.ReactElement) {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter>{node}</MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 20))
  })
}

function buttonByText(text: string): HTMLButtonElement {
  const found = [...host.querySelectorAll('button')].find(node => (node.textContent ?? '').includes(text))
  if (!found) throw new Error(`找不到按钮：${text}`)
  return found as HTMLButtonElement
}

async function click(node: HTMLButtonElement) {
  await act(async () => {
    node.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
  // mutation 落地 + onSuccess 跑完
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 40))
  })
}

async function typeInto(node: HTMLInputElement, value: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    setter.call(node, value)
    node.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

function inputByLabel(label: string): HTMLInputElement {
  const found = host.querySelector(`input[aria-label="${label}"]`)
  if (!found) throw new Error(`找不到输入框：${label}`)
  return found as HTMLInputElement
}

// ── report_submit ───────────────────────────────────────────

it('「生成研报」成功 → report_submit +1', async () => {
  const calls = stubRoutes({
    '/api/dsa/upstream/analysis/analyze': { body: { task_id: 't1' } },
    '/api/dsa/upstream/history': { body: { items: [] } },
  })
  await render(<Reports sample={false} reachable initialCode="600519" />)
  expect(readCoreActionCount()).toBe(0)

  await click(buttonByText('生成研报'))

  expect(calls.some(call => call.url === '/api/dsa/upstream/analysis/analyze')).toBe(true)
  expect(readCoreActionCount()).toBe(1)
  expect(readActionCount('report_submit')).toBe(1)
})

it('「生成研报」失败（500）→ 不 bump', async () => {
  stubRoutes({
    '/api/dsa/upstream/analysis/analyze': { status: 500, body: { detail: '上游炸了' } },
    '/api/dsa/upstream/history': { body: { items: [] } },
  })
  await render(<Reports sample={false} reachable initialCode="600519" />)
  await click(buttonByText('生成研报'))
  expect(readCoreActionCount()).toBe(0)
  expect(localStorage.getItem(DSA_FUNNEL_KEY_CORE_ACTION_COUNT)).toBeNull()
})

// ── sniper_register ─────────────────────────────────────────

it('登记狙击线 replaced=true → sniper_register +1', async () => {
  stubRoutes({
    '/api/dsa/upstream/history': { body: { items: [] } },
    '/api/dsa/sniper-lines': { body: { symbol: '600519.SH', lines: [{ kind: 'buy' }], replaced: true } },
  })
  await render(<Reports sample={false} reachable initialCode="600519" />)
  await typeInto(inputByLabel('理想买入价'), '1500')
  await click(buttonByText('登记狙击线'))

  expect(readCoreActionCount()).toBe(1)
  expect(readActionCount('sniper_register')).toBe(1)
  // 成功提示要说明这是价位线，不是监控规则
  expect(host.textContent).toContain('价位线')
})

it('登记狙击线 replaced=false 但 lines 非空 → 仍算成功，+1', async () => {
  stubRoutes({
    '/api/dsa/upstream/history': { body: { items: [] } },
    '/api/dsa/sniper-lines': { body: { symbol: '600519.SH', lines: [{ kind: 'buy' }], replaced: false } },
  })
  await render(<Reports sample={false} reachable initialCode="600519" />)
  await typeInto(inputByLabel('理想买入价'), '1500')
  await click(buttonByText('登记狙击线'))
  expect(readCoreActionCount()).toBe(1)
})

it('登记 replaced=false 且 lines 为空 → **不** bump，且如实提示没生效', async () => {
  stubRoutes({
    '/api/dsa/upstream/history': { body: { items: [] } },
    '/api/dsa/sniper-lines': { body: { symbol: '600519.SH', lines: [], replaced: false } },
  })
  await render(<Reports sample={false} reachable initialCode="600519" />)
  await typeInto(inputByLabel('理想买入价'), '1500')
  await click(buttonByText('登记狙击线'))

  expect(readCoreActionCount()).toBe(0)
  const message = host.querySelector('[data-dsa-sniper-message]')
  expect(message).not.toBeNull()
  expect(message!.getAttribute('data-dsa-sniper-message')).toBe('warn')
  expect(message!.textContent).toContain('没生效')
})

it('三价全空时前端就拦住，不发请求也不 bump', async () => {
  const calls = stubRoutes({
    '/api/dsa/upstream/history': { body: { items: [] } },
    '/api/dsa/sniper-lines': { body: { symbol: '600519.SH', lines: [], replaced: false } },
  })
  await render(<Reports sample={false} reachable initialCode="600519" />)
  await click(buttonByText('登记狙击线'))

  expect(calls.some(call => call.url === '/api/dsa/sniper-lines')).toBe(false)
  expect(readCoreActionCount()).toBe(0)
  expect(host.textContent).toContain('至少填一个价格')
})

it('不可达时登记按钮 disabled，并给出旁注', async () => {
  stubRoutes({ '/api/dsa/upstream/history': { body: { items: [] } } })
  await render(<Reports sample={false} reachable={false} initialCode="600519" />)
  const button = buttonByText('登记狙击线')
  expect(button.hasAttribute('disabled')).toBe(true)
  expect(host.querySelector('[data-dsa-sniper-gated]')).not.toBeNull()
})

// ── chat_send ───────────────────────────────────────────────

it('问股发送成功 → chat_send +1', async () => {
  stubRoutes({
    '/api/dsa/upstream/agent/chat': { body: { reply: '看趋势再说' } },
  })
  await render(<Chat reachable />)
  await typeInto(inputByLabel('问题'), '用趋势策略看 600519')
  await click(buttonByText('发送'))

  expect(readCoreActionCount()).toBe(1)
  expect(readActionCount('chat_send')).toBe(1)
  expect(host.textContent).toContain('看趋势再说')
})

it('问股失败 → 不 bump', async () => {
  stubRoutes({
    '/api/dsa/upstream/agent/chat': { status: 500, body: { detail: '上游炸了' } },
  })
  await render(<Chat reachable />)
  await typeInto(inputByLabel('问题'), '随便问一句')
  await click(buttonByText('发送'))
  expect(readCoreActionCount()).toBe(0)
})

// ── 三个动作互不串账 ────────────────────────────────────────

it('三个动作分别累加，总槽等于分键之和', async () => {
  stubRoutes({
    '/api/dsa/upstream/analysis/analyze': { body: { task_id: 't1' } },
    '/api/dsa/upstream/history': { body: { items: [] } },
    '/api/dsa/sniper-lines': { body: { symbol: '600519.SH', lines: [{ kind: 'buy' }], replaced: true } },
  })
  await render(<Reports sample={false} reachable initialCode="600519" />)
  await click(buttonByText('生成研报'))
  await typeInto(inputByLabel('理想买入价'), '1500')
  await click(buttonByText('登记狙击线'))

  expect(readActionCount('report_submit')).toBe(1)
  expect(readActionCount('sniper_register')).toBe(1)
  expect(readActionCount('chat_send')).toBe(0)
  expect(readCoreActionCount()).toBe(2)
})
