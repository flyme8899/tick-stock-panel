// @vitest-environment jsdom
/**
 * 登记狙击线的客户端契约（批次 20261025gdsa2 ③）。
 *
 * 钉三件事：
 * 1. 请求打到 `/api/dsa/sniper-lines`，method = POST，body 是 JSON；
 * 2. payload 的字段名与后端 `SniperLineRequest` 一致（snake_case，不转驼峰）；
 * 3. 「什么算成功」的判据只有一处定义 —— `isSniperRegisterSuccess`。
 *
 * 零真实网络：`fetch` 全部替换。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, expect, it, vi } from 'vitest'
import { DsaError, isSniperRegisterSuccess, registerSniperLines } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubJson(payload: unknown, status = 200) {
  // 显式声明参数，否则 vi.fn 推断出的调用元组是 []，mock.calls[0][0] 取不到值
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function bodyOf(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const init = fetchMock.mock.calls[0][1] as RequestInit
  return JSON.parse(String(init.body)) as Record<string, unknown>
}

it('打到 /api/dsa/sniper-lines，POST，JSON body', async () => {
  const fetchMock = stubJson({ symbol: '600519.SH', lines: [{ kind: 'buy' }], replaced: true })
  await registerSniperLines({ symbol: '600519', ideal_buy: 100 })
  expect(fetchMock.mock.calls[0][0]).toBe('/api/dsa/sniper-lines')
  const init = fetchMock.mock.calls[0][1] as RequestInit
  expect(init.method).toBe('POST')
  expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
})

it('payload 用后端字段名（ideal_buy / stop_loss / take_profit，不转驼峰）', async () => {
  const fetchMock = stubJson({ symbol: '600519.SH', lines: [], replaced: false })
  await registerSniperLines({
    symbol: '600519',
    ideal_buy: 100,
    stop_loss: 90,
    take_profit: 120,
  })
  const body = bodyOf(fetchMock)
  expect(body.symbol).toBe('600519')
  expect(body.ideal_buy).toBe(100)
  expect(body.stop_loss).toBe(90)
  expect(body.take_profit).toBe(120)
  // 别偷偷加后端不认识的键
  for (const camel of ['idealBuy', 'stopLoss', 'takeProfit']) {
    expect(body).not.toHaveProperty(camel)
  }
})

it('返回体原样透出 replaced 与 lines', async () => {
  stubJson({ symbol: '600519.SH', lines: [{ kind: 'buy' }, { kind: 'stop' }], replaced: true })
  const result = await registerSniperLines({ symbol: '600519' })
  expect(result.replaced).toBe(true)
  expect(result.lines).toHaveLength(2)
})

it('后端 400 抛 DsaError，并带上 detail', async () => {
  stubJson({ detail: '无法识别股票代码' }, 400)
  await expect(registerSniperLines({ symbol: '' })).rejects.toBeInstanceOf(DsaError)
  await expect(registerSniperLines({ symbol: '' })).rejects.toThrow('无法识别股票代码')
})

// ── 成功判据 ────────────────────────────────────────────────

it('replaced === true 算成功', () => {
  expect(isSniperRegisterSuccess({ symbol: '600519.SH', lines: [], replaced: true })).toBe(true)
})

it('lines.length > 0 算成功（即使 replaced 为 false）', () => {
  expect(isSniperRegisterSuccess({ symbol: '600519.SH', lines: [{ kind: 'buy' }], replaced: false })).toBe(true)
})

it('replaced === false 且 lines 为空 → 不算成功（这条最容易被当成成功）', () => {
  expect(isSniperRegisterSuccess({ symbol: '600519.SH', lines: [], replaced: false })).toBe(false)
})

it('判据只在 client 里定义一处：组件不自己拼 replaced/lines 判断', () => {
  // 反向证据：组件调用的是 isSniperRegisterSuccess，而不是内联 `result.replaced ||`
  const source = readFileSync(resolve(__dirname, 'DecisionWorkspace.tsx'), 'utf8')
  expect(source).toContain('isSniperRegisterSuccess(result)')
  expect(source).not.toMatch(/result\.replaced\s*\|\|/)
})
