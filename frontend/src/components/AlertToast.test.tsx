// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AlertEvent } from '@/lib/api'
import { AlertToastContainer, pushAlertToasts } from './AlertToast'
import { MonitorWidget } from './dashboard/widgets'

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    api: {
      ...actual.api,
      alertsList: vi.fn(async () => ({ alerts: sampleAlerts, total: sampleAlerts.length })),
      customSignalsList: vi.fn(async () => ({ signals: [] })),
      preferences: vi.fn(async () => ({})),
    },
  }
})

const sampleAlerts: AlertEvent[] = [
  {
    ts: 1,
    source: 'volume_delta',
    type: '轮询放量',
    symbol: '600000.SH',
    name: '浦发银行',
    message: '放量 · 单轮增量 9,500 手 >= 9,000 手 (间隔 3s)',
    signals: ['unknown_signal_key'],
    severity: 'warn',
  },
  {
    ts: 2,
    source: 'abnormal',
    type: 'abnormal_up',
    symbol: '600000.SH',
    name: '浦发银行',
    message: '浦发银行 3d偏离值 +1.60%',
    signals: [],
    severity: 'warn',
  },
]

let root: Root | undefined

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
})

afterEach(async () => {
  await act(async () => root?.unmount())
  root = undefined
  document.body.innerHTML = ''
})

function mount(node: ReactNode, seedAlerts = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  if (seedAlerts) {
    client.setQueryData(['alerts', ''], { alerts: sampleAlerts, total: sampleAlerts.length })
    client.setQueryData(['custom-signals'], { signals: [] })
  }
  const host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  act(() => {
    root?.render(
      <QueryClientProvider client={client}>
        <MemoryRouter>{node}</MemoryRouter>
      </QueryClientProvider>,
    )
  })
  return host
}

it('toast shows Chinese signal tags and rewrites interval text', async () => {
  const host = mount(<AlertToastContainer />)
  await act(async () => {
    pushAlertToasts(sampleAlerts)
  })
  const text = host.textContent ?? ''
  expect(text).toContain('放量')
  expect(text).toContain('异动')
  expect(text).toContain('间隔 3秒')
  expect(text).toContain('3日偏离')
  expect(text).not.toContain('volume_delta')
  expect(text).not.toContain('abnormal')
  expect(text).not.toContain('间隔 3s')
  const closers = host.querySelectorAll('button[aria-label="关闭通知"]')
  await act(async () => {
    closers.forEach(button => (button as HTMLButtonElement).click())
  })
})

it('monitor widget rows never show raw signal keys', async () => {
  const host = mount(<MonitorWidget onStockClick={() => {}} />, true)
  const text = host.textContent ?? ''
  expect(text).toContain('放量')
  expect(text).toContain('异动')
  expect(text).toContain('间隔 3秒')
  expect(text).toContain('3日偏离')
  expect(text).toContain('其他')
  expect(text).not.toContain('volume_delta')
  expect(text).not.toContain('abnormal')
  expect(text).not.toContain('unknown_signal_key')
  expect(text).not.toContain('间隔 3s')
})
