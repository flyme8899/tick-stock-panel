// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SectorAnalysis } from './SectorAnalysis'

vi.mock('@/components/SectorRotationCard', () => ({
  SectorRotationCard: ({ kind }: { kind: string }) => <div data-rotation={kind}>板块切换</div>,
}))

vi.mock('@/lib/api', () => ({
  api: {
    extDataList: async () => ({
      items: [
        {
          id: 'ext_gn_ths',
          label: '扩展概念',
          description: '同花顺概念分类',
          fields: [
            { name: 'symbol', dtype: 'str', label: '代码' },
            { name: '所属概念', dtype: 'str', label: '所属概念' },
          ],
        },
        {
          id: 'ext_hy_ths',
          label: '扩展行业',
          description: '同花顺行业分类',
          fields: [
            { name: 'symbol', dtype: 'str', label: '代码' },
            { name: '所属同花顺行业', dtype: 'str', label: '所属同花顺行业' },
          ],
        },
      ],
    }),
    extDataRows: async (id: string) => {
      if (id === 'ext_gn_ths') {
        return {
          id,
          total: 2,
          fields: [
            { name: 'symbol', dtype: 'str', label: '代码' },
            { name: '所属概念', dtype: 'str', label: '所属概念' },
          ],
          rows: [
            { symbol: '000001.SZ', name: '平安银行', 所属概念: '银行概念' },
            { symbol: '000002.SZ', name: '万科A', 所属概念: '地产概念' },
          ],
        }
      }
      return {
        id,
        total: 3,
        fields: [
          { name: 'symbol', dtype: 'str', label: '代码' },
          { name: '所属同花顺行业', dtype: 'str', label: '所属同花顺行业' },
        ],
        rows: [
          { symbol: '000001.SZ', name: '平安银行', 所属同花顺行业: '银行-国有大行-大型' },
          { symbol: '600000.SH', name: '浦发银行', 所属同花顺行业: '银行-股份制-中型' },
          { symbol: '000002.SZ', name: '万科A', 所属同花顺行业: '房地产-住宅开发-全国' },
        ],
      }
    },
    marketSnapshot: async () => ({
      as_of: '2026-10-10 15:00',
      rows: [
        { symbol: '000001.SZ', name: '平安银行', change_pct: 0.02, amount: 1e9, turnover_rate: 1.2, vol_ratio_5d: 1.1 },
        { symbol: '600000.SH', name: '浦发银行', change_pct: -0.01, amount: 8e8, turnover_rate: 0.8, vol_ratio_5d: 0.9 },
        { symbol: '000002.SZ', name: '万科A', change_pct: 0.03, amount: 5e8, turnover_rate: 2, vol_ratio_5d: 1.4 },
      ],
    }),
    extDataSchemaAll: async () => ({ items: [] }),
    extDataPresetFetch: async () => ({ ok: true }),
  },
}))

function LocationProbe() {
  const location = useLocation()
  return <div data-loc>{location.pathname}{location.search}</div>
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  localStorage.clear()
  localStorage.setItem('industry-analysis-config', JSON.stringify({ hierarchyLevel: 1 }))
  localStorage.setItem('concept-analysis-config', JSON.stringify({ dimensionField: '所属概念' }))
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  localStorage.clear()
})

async function settle() {
  for (let i = 0; i < 8; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

function renderAt(path: string) {
  act(() => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <QueryClientProvider client={client}>
          <SectorAnalysis />
          <LocationProbe />
        </QueryClientProvider>
      </MemoryRouter>,
    )
  })
}

function tab(name: string) {
  return [...host.querySelectorAll('[role="tab"]')].find(el => el.textContent === name) as HTMLButtonElement
}

it('defaults to the concept tab and keeps industry-only pieces off it', async () => {
  renderAt('/sector-analysis')
  await settle()

  expect(host.querySelector('h1')?.textContent).toBe('板块分析')
  expect(tab('概念').getAttribute('aria-selected')).toBe('true')
  expect(tab('行业').getAttribute('aria-selected')).toBe('false')
  expect(host.textContent).toContain('概念矩阵')
  expect(host.textContent).toContain('银行概念')
  expect(host.textContent).not.toContain('热度分布')
  expect(host.textContent).not.toContain('级行业')
  expect(host.querySelector('[data-rotation]')?.getAttribute('data-rotation')).toBe('concept')

  await act(async () => {
    host.querySelector<HTMLButtonElement>('button[title="配置数据源"]')?.click()
  })
  await settle()
  expect(host.textContent).not.toContain('统计层级')
  expect(host.textContent).not.toContain('一级行业')
})

it('shows hierarchy grouping and the heatmap only on the industry tab', async () => {
  renderAt('/sector-analysis?tab=industry')
  await settle()

  expect(host.querySelector('[data-loc]')?.textContent).toBe('/sector-analysis?tab=industry')
  expect(tab('行业').getAttribute('aria-selected')).toBe('true')
  expect(host.textContent).toContain('1级行业')
  expect(host.textContent).toContain('行业矩阵')
  expect(host.textContent).toContain('热度分布')
  expect(host.textContent).toContain('银行')
  expect(host.textContent).not.toContain('国有大行')
  expect(host.textContent).not.toContain('银行概念')
  expect(host.querySelector('[data-rotation]')?.getAttribute('data-rotation')).toBe('industry')

  await act(async () => {
    host.querySelector<HTMLButtonElement>('button[title="配置数据源"]')?.click()
  })
  await settle()
  expect(host.textContent).toContain('统计层级')
  expect(host.textContent).toContain('一级行业')

  const conceptConfig = localStorage.getItem('concept-analysis-config')
  await act(async () => {
    ;[...host.querySelectorAll('button')].find(el => el.textContent === '保存')?.click()
  })
  await settle()
  expect(localStorage.getItem('concept-analysis-config')).toBe(conceptConfig)
  expect(JSON.parse(localStorage.getItem('industry-analysis-config') || '{}').hierarchyLevel).toBe(1)

  await act(async () => { tab('概念').click() })
  await settle()
  expect(host.querySelector('[data-loc]')?.textContent).toBe('/sector-analysis?tab=concept')
  expect(host.textContent).toContain('概念矩阵')
  expect(host.textContent).toContain('银行概念')
  expect(host.textContent).not.toContain('热度分布')
  expect(host.textContent).not.toContain('1级行业')
})
