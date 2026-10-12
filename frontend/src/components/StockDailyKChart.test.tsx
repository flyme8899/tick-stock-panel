// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { CandleMarker, KlineRow } from '@/lib/api'

const state = vi.hoisted(() => ({
  rows: [] as KlineRow[],
  candle_markers: [] as CandleMarker[],
}))

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({
    data: { rows: state.rows, candle_markers: state.candle_markers, stock_info: { name: '西部矿业' } },
    isLoading: false,
    isError: false,
  }),
}))

vi.mock('@/components/EChartsCandlestick', () => ({
  EChartsCandlestick: ({ markers }: { markers?: Array<{ label?: string; patternTag?: boolean }> }) => (
    <div data-testid="marks">
      {(markers ?? []).filter(marker => marker.patternTag).map(marker => marker.label).join('|')}
    </div>
  ),
  OVERLAY_INDICATORS: [],
  SUB_CHARTS: [],
}))

import { StockDailyKChart } from './StockDailyKChart'

function bars(count: number): KlineRow[] {
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(Date.UTC(2026, 7, 10 + index)).toISOString().slice(0, 10)
    return { date, open: 10, high: 11, low: 9, close: 10.4, volume: 1000 }
  })
}

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  state.rows = bars(16)
  state.candle_markers = [
    { date: state.rows[2].date as string, side: 'bull', name: '长蜡烛·纺锤', patterns: ['长蜡烛', '纺锤'] },
    { date: state.rows[3].date as string, side: 'bull', name: '短蜡烛', patterns: ['短蜡烛'] },
    { date: state.rows[10].date as string, side: 'bear', name: '吞没', patterns: ['吞没'] },
  ]
  localStorage.clear()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

function patternButton(): HTMLButtonElement {
  const button = [...host.querySelectorAll('button')].find(item => item.textContent === '形态')
  if (!button) throw new Error('找不到形态按钮')
  return button
}

async function renderChart() {
  await act(async () => root.render(<StockDailyKChart symbol="601168.SH" showMA={false} />))
}

it('形态默认关闭，刷新后仍关闭，打开后只显示重要短标签', async () => {
  await renderChart()
  const button = patternButton()
  expect(button.className).not.toContain('bg-[#C74040]/10')
  expect(button.title).toContain('显示')
  expect(host.querySelector('[data-testid="marks"]')?.textContent).toBe('')

  await act(async () => { button.click() })
  expect(patternButton().className).toContain('bg-[#C74040]/10')
  expect(host.querySelector('[data-testid="marks"]')?.textContent).toBe('吞没')

  await act(async () => root.unmount())
  root = createRoot(host)
  await renderChart()
  expect(patternButton().className).not.toContain('bg-[#C74040]/10')
  expect(host.querySelector('[data-testid="marks"]')?.textContent).toBe('')
})
