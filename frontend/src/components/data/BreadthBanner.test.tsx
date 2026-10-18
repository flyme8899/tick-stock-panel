// @vitest-environment jsdom
// 市场宽度提醒横幅: 低档醒目 / 中档弱提示 / 高档与无数据不渲染。
// 用 mock 掉 api.regimeBreadthLatest 控制返回, 断言渲染结果文案与是否出现。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const latestMock = vi.fn()
vi.mock('@/lib/api', () => ({
  api: { regimeBreadthLatest: () => latestMock() },
}))

import { BreadthBanner } from './BreadthBanner'

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
  latestMock.mockReset()
})

async function render() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  await act(async () => {
    root!.render(
      <QueryClientProvider client={qc}>
        <BreadthBanner />
      </QueryClientProvider>,
    )
  })
  // 等查询 promise 落地并触发重渲染(轮询若干微任务, 消除时序抖动)
  for (let i = 0; i < 10; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

const baseResp = (point: unknown) => ({
  latest: point,
  note: '研究备注 · 非交易信号',
  threshold_source: '研究前段(≤2026-04-01)分位 · 冻结',
})

it('低档: 显示醒目风险提醒文案', async () => {
  latestMock.mockResolvedValue(baseResp({ date: '2026-04-10', v_breadth: 1.4, bucket: 'low', suggested_pos: 0.3 }))
  await render()
  expect(container.textContent).toContain('市场宽度转弱')
  expect(container.textContent).toContain('建议降低总仓位')
  expect(container.textContent).toContain('非买卖信号')
})

it('中档: 只显示弱提示, 不劝动作', async () => {
  latestMock.mockResolvedValue(baseResp({ date: '2026-04-10', v_breadth: 1.6, bucket: 'mid', suggested_pos: 0.6 }))
  await render()
  expect(container.textContent).toContain('市场宽度中性')
  expect(container.textContent).not.toContain('建议降低总仓位')
})

it('高档: 不渲染任何内容(无骚扰)', async () => {
  latestMock.mockResolvedValue(baseResp({ date: '2026-04-10', v_breadth: 1.8, bucket: 'high', suggested_pos: 1.0 }))
  await render()
  expect(container.textContent).toBe('')
})

it('无数据/窗口不足(bucket=null): 不渲染', async () => {
  latestMock.mockResolvedValue(baseResp(null))
  await render()
  expect(container.textContent).toBe('')
})

it('首日切换: 仍显示当天低档, 旁边加注第 1/3 天', async () => {
  latestMock.mockResolvedValue({
    ...baseResp({ date: '2026-04-10', v_breadth: 1.4, bucket: 'low', suggested_pos: 0.3 }),
    confirm_days: 1,
  })
  await render()
  expect(container.textContent).toContain('市场宽度转弱')
  expect(container.textContent).toContain('建议降低总仓位')
  expect(container.textContent).toContain('档位确认中（第 1/3 天）')
})

it('第 2/3 天: 中档照常显示, 加注第 2/3 天, 不改劝仓文案', async () => {
  latestMock.mockResolvedValue({
    ...baseResp({ date: '2026-04-11', v_breadth: 1.6, bucket: 'mid', suggested_pos: 0.6 }),
    confirm_days: 2,
  })
  await render()
  expect(container.textContent).toContain('市场宽度中性')
  expect(container.textContent).toContain('档位确认中（第 2/3 天）')
  expect(container.textContent).not.toContain('建议降低总仓位')
})

it('满 3 天后: confirm_days 为 null, 去掉标注, 当天档位仍在', async () => {
  latestMock.mockResolvedValue({
    ...baseResp({ date: '2026-04-12', v_breadth: 1.4, bucket: 'low', suggested_pos: 0.3 }),
    confirm_days: null,
  })
  await render()
  expect(container.textContent).toContain('市场宽度转弱')
  expect(container.textContent).not.toContain('档位确认中')
})

it('切回原档位: confirm_days 为 null, 不加标注', async () => {
  latestMock.mockResolvedValue({
    ...baseResp({ date: '2026-04-12', v_breadth: 1.6, bucket: 'mid', suggested_pos: 0.6 }),
    confirm_days: null,
  })
  await render()
  expect(container.textContent).toContain('市场宽度中性')
  expect(container.textContent).not.toContain('档位确认中')
})

it('高档即使处于确认天数也不渲染横幅', async () => {
  latestMock.mockResolvedValue({
    ...baseResp({ date: '2026-04-10', v_breadth: 1.8, bucket: 'high', suggested_pos: 1.0 }),
    confirm_days: 1,
  })
  await render()
  expect(container.textContent).toBe('')
})
