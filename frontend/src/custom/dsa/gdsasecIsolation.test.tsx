// @vitest-environment jsdom
/**
 * P-sec（批次 20261026gdsasec）：浏览器上游只读闸的前端一侧。
 *
 * 仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
 *
 * 后端把 `/api/dsa/upstream` 对 `system`/`portfolio` 收窄成只读之后，
 * Schedule 上「立即跑一轮」「发送测试」这两个按钮就再也发不出去了。
 * 这里钉的是**诚实**：
 *
 * - 两个按钮恒 `disabled`（不是「点了才知道 405」）
 * - 页面明说为什么，且指向「服务端入口」而不是含糊其辞
 * - 源码里那两条 system POST 的调用点已经撤掉
 * - 不偷偷改打别的写 API、不走 MCP、不改三 env
 * - 定时设置保存走 TSP 自有接口，**没**被误伤
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Schedule } from './DecisionWorkspace'

const SOURCE = readFileSync(resolve(__dirname, 'DecisionWorkspace.tsx'), 'utf8')

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
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const schedulePayload = {
  enabled: true,
  time: '18:00',
  trading_days_only: true,
  region: 'cn',
  watchlist: '',
  extra_times: [],
  scheduler: {
    enabled: true,
    running: false,
    next_run_at: '2026-10-27T18:00:00+08:00',
    last_success_at: '2026-10-24T18:00:00+08:00',
    last_skip_reason: '',
    last_error: '',
  },
}

function stubFetch(payload: unknown) {
  const calls: { url: string; method: string }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), method: (init?.method ?? 'GET').toUpperCase() })
    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    } as unknown as Response
  }))
  return calls
}

async function renderSchedule(reachable = true) {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <Schedule reachable={reachable} />
      </QueryClientProvider>,
    )
  })
  for (let i = 0; i < 20; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    if (!(host.textContent ?? '').includes('加载中')) return
  }
}

function buttonByText(text: string): HTMLButtonElement | null {
  const all = Array.from(host.querySelectorAll('button'))
  return (all.find(b => (b.textContent ?? '').trim() === text) as HTMLButtonElement | undefined) ?? null
}

describe('N1 · 两个 system 写按钮诚实禁用', () => {
  it('「立即跑一轮」恒 disabled，且带只读说明', async () => {
    stubFetch(schedulePayload)
    await renderSchedule(true)

    const runBtn = buttonByText('立即跑一轮')
    expect(runBtn).not.toBeNull()
    expect(runBtn!.disabled).toBe(true)
    expect(runBtn!.getAttribute('data-upstream-readonly')).toBe('system/scheduler/run-now')
  })

  it('「发送测试」恒 disabled，且带只读说明', async () => {
    stubFetch(schedulePayload)
    await renderSchedule(true)

    const testBtn = buttonByText('发送测试')
    expect(testBtn).not.toBeNull()
    expect(testBtn!.disabled).toBe(true)
    expect(testBtn!.getAttribute('data-upstream-readonly')).toBe('system/config/notification/test-channel')
  })

  it('reachable=true 时也仍然禁用（不是「连上了就能点」）', async () => {
    stubFetch(schedulePayload)
    await renderSchedule(true)
    // 服务是通的（能读到调度状态），按钮依旧灰
    expect((host.textContent ?? '').includes('调度已启动')).toBe(true)
    expect(buttonByText('立即跑一轮')!.disabled).toBe(true)
    expect(buttonByText('发送测试')!.disabled).toBe(true)
  })

  it('点击被禁用的按钮不会发出任何 system 写请求', async () => {
    const calls = stubFetch(schedulePayload)
    await renderSchedule(true)
    const before = calls.length

    const runBtn = buttonByText('立即跑一轮')!
    const testBtn = buttonByText('发送测试')!
    await act(async () => { runBtn.click(); testBtn.click() })

    const after = calls.slice(before)
    expect(after.filter(c => c.url.includes('run-now'))).toEqual([])
    expect(after.filter(c => c.url.includes('test-channel'))).toEqual([])
    expect(after.filter(c => c.method !== 'GET')).toEqual([])
  })
})

describe('N1 · 文案：说明为什么、指向哪里', () => {
  it('明说「浏览器上游已收窄为只读」', async () => {
    stubFetch(schedulePayload)
    await renderSchedule(true)
    expect(host.textContent ?? '').toContain('浏览器上游已收窄为只读')
  })

  it('指向服务端入口，而不是含糊其辞', async () => {
    stubFetch(schedulePayload)
    await renderSchedule(true)
    const text = host.textContent ?? ''
    expect(text).toContain('服务端入口')
  })

  it('说清定时设置保存不受影响（避免用户以为整个页面死了）', async () => {
    stubFetch(schedulePayload)
    await renderSchedule(true)
    expect(host.textContent ?? '').toContain('定时设置保存')
  })
})

describe('源码级：POST 调用点已撤、未偷换旁路', () => {
  it('Schedule 里不再有 system/scheduler/run-now 的 dsaUpstream 调用', () => {
    expect(SOURCE).not.toContain("dsaUpstream('system/scheduler/run-now'")
  })

  it('Schedule 里不再有 test-channel 的 dsaUpstream 调用', () => {
    expect(SOURCE).not.toContain("dsaUpstream('system/config/notification/test-channel'")
  })

  it('没有偷换成别的写 API 或 MCP 旁路', () => {
    // 路径名仍会出现在 data-upstream-readonly 标注里（那是给人看「为什么灰」的），
    // 但不许再作为**请求**出现：不能有 dsaUpstream / fetch 打过去。
    expect(SOURCE).not.toContain("dsaUpstream('system/scheduler/run-now'")
    expect(SOURCE).not.toContain("dsaUpstream('system/config/notification/test-channel'")
    expect(SOURCE).not.toContain('/api/dsa/sec-')
    // 也不许经 MCP 或别的前缀绕过去触发同一件事
    expect(SOURCE).not.toContain('mcp')
  })

  it('定时设置保存仍走 TSP 自有接口（未被误伤）', () => {
    expect(SOURCE).toContain('saveDsaSchedule({')
    // 未在源码里改三 env
    expect(SOURCE).not.toContain('BUY_SELL_SIGNALS_PERSIST')
    expect(SOURCE).not.toContain('BUY_SELL_SIGNALS_PUSH')
    expect(SOURCE).not.toContain('BUY_SELL_SIGNALS_DINGTALK')
  })

  it('未动 DSA_REVIEW_* / 狙击前缀', () => {
    expect(SOURCE).not.toContain('DSA_REVIEW_DINGTALK')
    expect(SOURCE).not.toContain('ALERT_SOURCE_PREFIX')
  })
})

describe('Portfolio 空态：只有 sample===true 才用样例', () => {
  it('Portfolio 的样例分支被 sample 单独把守', () => {
    // reachable ? risk.data : sample ? SAMPLE_RISK : null —— 中间那层不能省
    expect(SOURCE).toContain('reachable ? risk.data : sample ? SAMPLE_RISK : null')
  })

  it('不可达时走 EmptyState，不渲染样例数据', () => {
    expect(SOURCE).toContain('{!reachable && !sample && <EmptyState icon={PieChart}')
  })
})
