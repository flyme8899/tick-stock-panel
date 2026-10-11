// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { DecisionSignalsPanel } from './DecisionWorkspace'

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

const stats = {
  engine_version: 'decision-signal-v1',
  horizons: ['1d', '3d', '5d', '10d'],
  statuses: ['active', 'expired'],
  total: 0,
  completed: 0,
  unable: 0,
  hit: 0,
  miss: 0,
  neutral: 0,
}

async function renderPanel(overrides: Partial<Parameters<typeof DecisionSignalsPanel>[0]> = {}) {
  await act(async () => {
    root.render(
      <DecisionSignalsPanel
        code="600519"
        onCodeChange={() => undefined}
        onSubmit={() => undefined}
        reachable
        pending={false}
        error={null}
        stats={stats}
        list={{ items: [], total: 0, page: 1, page_size: 20 }}
        outcomePages={[]}
        {...overrides}
      />,
    )
  })
}

it('uses an empty state when the stock has no signals and keeps raw json collapsed', async () => {
  await renderPanel()
  expect(host.textContent).toContain('该股暂无决策信号')
  expect(host.textContent).toContain('分析生成后会自动记录。')
  const raw = host.querySelector('[data-signal-raw]')
  expect(raw).toBeInstanceOf(HTMLDetailsElement)
  expect((raw as HTMLDetailsElement).open).toBe(false)
  expect(raw?.querySelector('summary')?.textContent).toBe('原始数据')
  expect(raw?.querySelector('pre')?.textContent).toContain('"total": 0')
  expect(host.querySelector(':scope > pre, [data-signal-row]')).toBeNull()
})

it('renders direction, horizon, status, created time, and outcome', async () => {
  await renderPanel({
    stats: { ...stats, total: 1, completed: 1, hit: 1 },
    list: {
      items: [
        {
          id: 7,
          action: 'buy',
          action_label: '买入',
          horizon: '5d',
          status: 'active',
          created_at: '2026-10-10T09:30:00',
        },
        {
          id: 8,
          action: 'sell',
          horizon: '1d',
          status: 'expired',
          created_at: '2026-10-09T15:00:00+08:00',
        },
      ],
      total: 2,
      page: 1,
      page_size: 20,
    },
    outcomePages: [
      { items: [{ signal_id: 7, horizon: '5d', outcome: 'hit', eval_status: 'completed' }] },
      { items: [{ signal_id: 8, horizon: '1d', outcome: 'miss', eval_status: 'completed' }] },
    ],
  })

  const rows = [...host.querySelectorAll('[data-signal-row]')]
  expect(rows).toHaveLength(2)
  expect(rows[0].querySelector('[data-signal-direction]')?.textContent).toBe('买入')
  expect(rows[0].querySelector('[data-signal-direction]')?.className).toContain('text-bull')
  expect(rows[0].querySelector('[data-signal-horizon]')?.textContent).toBe('5 日')
  expect(rows[0].querySelector('[data-signal-status]')?.textContent).toBe('生效中')
  expect(rows[0].querySelector('[data-signal-created]')?.textContent).toBe('2026-10-10 09:30')
  expect(rows[0].querySelector('[data-signal-outcome]')?.textContent).toBe('命中')
  expect(rows[0].querySelector('[data-signal-outcome]')?.className).toContain('text-bull')
  expect(rows[1].querySelector('[data-signal-direction]')?.className).toContain('text-bear')
  expect(rows[1].querySelector('[data-signal-outcome]')?.textContent).toBe('未中')
  expect(rows[1].querySelector('[data-signal-outcome]')?.className).toContain('text-bear')
  expect(host.textContent).not.toContain('该股暂无决策信号')
  expect((host.querySelector('[data-signal-raw]') as HTMLDetailsElement).open).toBe(false)
  expect(host.querySelector('[data-signal-raw] pre')?.textContent).toContain('decision-signal-v1')
})

it('says the outcome has not been reviewed when no outcome row exists', async () => {
  await renderPanel({
    list: {
      items: [{ id: 3, action: 'watch', horizon: '3d', status: 'active', created_at: '2026-10-01T08:00:00' }],
      total: 1,
      page: 1,
      page_size: 20,
    },
  })
  const row = host.querySelector('[data-signal-row]')
  expect(row?.querySelector('[data-signal-direction]')?.textContent).toBe('观望')
  expect(row?.querySelector('[data-signal-outcome]')?.textContent).toBe('尚未复盘')
  expect(row?.querySelector('[data-signal-outcome]')?.className).toContain('text-muted')
})
