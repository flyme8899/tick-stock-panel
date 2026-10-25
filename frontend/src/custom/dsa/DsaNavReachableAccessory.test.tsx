// @vitest-environment jsdom
/**
 * 导航「决策」可达状态点测试（批次 20261025gdsa）。
 *
 * 重点防一件事：`enabled === true` 时**不得**画成「已连接 / 已就绪」。
 * 后端 `enabled()` 约等于 `bool(DSA_BASE_URL)`，默认 `.env` 下近乎恒真，
 * 拿它当可用性会把人送进「看起来坏了」的决策页。
 *
 * 零网络：`fetch` 被替换成返回固定 status。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DsaNavReachableAccessory } from './DsaNavReachableAccessory'

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
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function renderAccessory() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <DsaNavReachableAccessory />
      </QueryClientProvider>,
    )
  })
  // 等 useQuery 的 fetch 完成并重渲染
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 30))
  })
  return host.querySelector('[data-dsa-nav-reachable]')
}

it('reachable=true → 已连接态', async () => {
  stubStatus({ enabled: true, base_url: 'http://x', reachable: true, detail: '' })
  const node = await renderAccessory()
  expect(node).not.toBeNull()
  expect(node!.getAttribute('data-dsa-nav-reachable')).toBe('true')
  expect(node!.getAttribute('data-dsa-nav-reachable-label')).toBe('已连接')
})

it('reachable=false → 未连接态，并把 detail 放进 title', async () => {
  stubStatus({ enabled: true, base_url: 'http://x', reachable: false, detail: 'connect ECONNREFUSED' })
  const node = await renderAccessory()
  expect(node!.getAttribute('data-dsa-nav-reachable')).toBe('false')
  expect(node!.getAttribute('data-dsa-nav-reachable-label')).toBe('未连接')
  expect(node!.getAttribute('title')).toBe('connect ECONNREFUSED')
})

it('enabled=true 但 reachable=false → 绝不显示为已连接', async () => {
  stubStatus({ enabled: true, base_url: 'http://x', reachable: false, detail: '' })
  const node = await renderAccessory()
  expect(node!.getAttribute('data-dsa-nav-reachable')).toBe('false')
  expect(node!.getAttribute('data-dsa-nav-reachable-label')).not.toBe('已连接')
  // 也不出现「已连接」以外的就绪措辞
  for (const word of ['就绪', '可用', '正常']) {
    expect(node!.getAttribute('data-dsa-nav-reachable-label')).not.toContain(word)
  }
})

it('enabled=true 且 reachable 字段缺失 → 仍是「检测中」，不冒充已连接', async () => {
  stubStatus({ enabled: true, base_url: 'http://x', detail: '' })
  const node = await renderAccessory()
  expect(node!.getAttribute('data-dsa-nav-reachable')).toBe('unknown')
  expect(node!.getAttribute('data-dsa-nav-reachable-label')).toBe('检测中')
})

it('请求失败 → 检测中（不给假结论）', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('boom') }))
  const node = await renderAccessory()
  expect(node!.getAttribute('data-dsa-nav-reachable')).toBe('unknown')
  expect(node!.getAttribute('data-dsa-nav-reachable-label')).toBe('检测中')
})

it('命中共享的 dsa-status 缓存键（不与页内重复轮询）', async () => {
  const fetchMock = stubStatus({ enabled: true, base_url: 'http://x', reachable: true, detail: '' })
  await renderAccessory()
  expect(fetchMock).toHaveBeenCalledWith('/api/dsa/status')
  // 缓存里应存在 dsa-status 这一项
  expect(client.getQueryData(['dsa-status'])).toBeTruthy()
})
