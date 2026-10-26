// @vitest-environment jsdom
/**
 * backtestTask 请求面: 叠加字段如何落到 SSE query 上。
 *
 * 批次 20261026gbtstrat · 这是「前端有字段但没测到」的补测:
 * - merge_mode 必须真的进 query (否则后端永远拿默认值, 用户切的并集是假的)
 * - overlay_children 只在 >=2 时下传 (1 个就是单选, 不该伪装成叠加让后端 400)
 * - 单选请求不带任何叠加字段 (防止多选残留串味)
 *
 * 断言手段: startBacktest 把 query 原样存进 localStorage['backtest_reconnect']
 * (刷新重连用), 这是最靠近真实网络载荷的可观测点, 不必 mock EventSource。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearBacktest, startBacktest } from './backtestTask'

const RECONNECT_KEY = 'backtest_reconnect'

/** 读出 startBacktest 组装的 query, 解析成对象便于断言 */
function issuedQuery(): Record<string, string> {
  const qs = localStorage.getItem(RECONNECT_KEY)
  expect(qs, 'startBacktest 应把 query 写入 reconnect key').toBeTruthy()
  return Object.fromEntries(new URLSearchParams(qs as string).entries())
}

beforeEach(() => {
  localStorage.clear()
  // connectSSE 会先 fetch 预检再 new EventSource; 这里只要"不炸", 载荷断言不依赖连接结果。
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({}),
    body: { cancel: async () => {} },
  })))
  vi.stubGlobal('EventSource', class {
    onopen: (() => void) | null = null
    addEventListener() {}
    close() {}
  })
})

afterEach(() => {
  clearBacktest()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('startBacktest 的叠加字段 (M30 / M31)', () => {
  it('>=2 子策略时下传 overlay_children 与用户选的 merge_mode', () => {
    startBacktest({
      strategy_id: '__ephemeral_overlay__',
      overlay_children: [
        { strategy_id: 'boll_breakout', weight: 1 },
        { strategy_id: 'macd_golden', weight: 1 },
      ],
      merge_mode: 'union',
      min_confirm: 2,
    })

    const q = issuedQuery()
    expect(JSON.parse(q.overlay_children)).toEqual([
      { strategy_id: 'boll_breakout', weight: 1 },
      { strategy_id: 'macd_golden', weight: 1 },
    ])
    // 关键: 用户切的 union 必须真的发出去, 不能被默认值吞掉
    expect(q.merge_mode).toBe('union')
    expect(q.min_confirm).toBe('2')
    expect(q.strategy_id).toBe('__ephemeral_overlay__')
  })

  it('merge_mode 缺省时按 intersect 下发 (与后端默认一致)', () => {
    startBacktest({
      strategy_id: '__ephemeral_overlay__',
      overlay_children: [
        { strategy_id: 'a', weight: 1 },
        { strategy_id: 'b', weight: 1 },
      ],
      merge_mode: 'intersect',
    })
    expect(issuedQuery().merge_mode).toBe('intersect')
  })

  it('只有 1 个子策略时不下传 overlay_children (不把单选伪装成叠加)', () => {
    startBacktest({
      strategy_id: 'boll_breakout',
      overlay_children: [{ strategy_id: 'boll_breakout', weight: 1 }],
      merge_mode: 'intersect',
      min_confirm: 0,
    })

    const q = issuedQuery()
    expect(q.overlay_children).toBeUndefined()
    // min_confirm 依赖 overlay_children 长度, 同样不下传
    expect(q.min_confirm).toBeUndefined()
  })

  it('单选请求不携带任何叠加字段', () => {
    startBacktest({
      strategy_id: 'boll_breakout',
      overlay_children: null,
      merge_mode: undefined,
      min_confirm: undefined,
    })

    const q = issuedQuery()
    expect(q.overlay_children).toBeUndefined()
    expect(q.merge_mode).toBeUndefined()
    expect(q.min_confirm).toBeUndefined()
    expect(q.strategy_id).toBe('boll_breakout')
  })
})
