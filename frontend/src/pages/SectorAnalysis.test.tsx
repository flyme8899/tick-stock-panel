// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SectorAnalysis } from './SectorAnalysis'

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
    sectorRotation: async (params: { kind: string }) => ({
      status: 'ok',
      date: '2026-10-10',
      as_of: '15:00',
      timeline: [{
        time: '15:00',
        rotation: params.kind === 'industry' ? 0.31 : 0.42,
        leader: params.kind === 'industry' ? '银行' : '银行概念',
        leader_pct: 0.02,
        market_pct: 0.01,
      }],
      sectors: [{
        name: params.kind === 'industry' ? '银行' : '银行概念',
        pct_now: 0.02,
        pct_prev: 0.01,
        rank_now: 1,
        rank_prev: 2,
        rank_change: 1,
        flow: null,
        score: 1,
        n_members: 2,
        n_members_with_bars: 2,
      }],
    }),
    rpsRotation: async () => ({
      dates: ['2026-10-09', '2026-10-10'],
      columns: {
        '2026-10-09': [['银行概念', 0.01], ['地产概念', -0.01], ['银行', 0.015]],
        '2026-10-10': [['银行概念', 0.02], ['银行', 0.01], ['地产概念', 0.005]],
      },
      concept_count: 3,
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
  delete (window as { matchMedia?: unknown }).matchMedia
})

async function settle() {
  for (let i = 0; i < 10; i++) {
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

function column(kind: string) {
  const node = host.querySelector(`[data-kind="${kind}"]`)
  if (!node) throw new Error(`missing column ${kind}`)
  return node
}

function useNarrowViewport(matches: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: matches && query.includes('max-width'),
    media: query,
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return false },
    onchange: null,
    addListener() {},
    removeListener() {},
  })) as unknown as typeof window.matchMedia
}

it('shows concept and industry side by side, with industry-only level and heatmap', async () => {
  renderAt('/sector-analysis')
  await settle()

  expect(host.querySelector('h1')?.textContent).toBe('板块分析')
  expect(host.querySelector('[role="tablist"]')).toBeNull()
  expect(column('concept').textContent).toContain('银行概念')
  expect(column('concept').textContent).toContain('概念轮动')
  expect(column('concept').textContent).toContain('切换强度 0.42')
  expect(column('concept').textContent).not.toContain('热度分布')
  expect(column('concept').querySelector('[aria-label="行业层级"]')).toBeNull()

  expect(column('industry').textContent).toContain('1级行业')
  expect(column('industry').textContent).toContain('热度分布')
  expect(column('industry').textContent).toContain('银行')
  expect(column('industry').textContent).toContain('行业轮动')
  expect(column('industry').textContent).not.toContain('国有大行')
  expect(column('industry').textContent).not.toContain('银行概念')
  expect(host.querySelectorAll('[data-rotation]')).toHaveLength(2)
  expect(host.querySelector('#sector-detail')?.textContent).toContain('点击上方概念或行业')
})

it('opens one shared detail for whichever column is clicked', async () => {
  renderAt('/sector-analysis')
  await settle()

  await act(async () => {
    column('concept').querySelector<HTMLButtonElement>('[data-sector="银行概念"]')?.click()
  })
  await settle()

  const detail = host.querySelector('#sector-detail')
  expect(detail?.getAttribute('data-detail-kind')).toBe('concept')
  expect(detail?.textContent).toContain('平安银行')
  expect(detail?.textContent).toContain('本概念三龙头')
  expect(detail?.textContent).toContain('涨幅 RPS')
  expect(detail?.textContent).toContain('#1')
  expect(detail?.textContent).not.toContain('级行业')

  await act(async () => {
    column('industry').querySelector<HTMLButtonElement>('[data-sector="银行"]')?.click()
  })
  await settle()
  expect(host.querySelector('#sector-detail')?.getAttribute('data-detail-kind')).toBe('industry')
  expect(host.querySelector('#sector-detail')?.textContent).toContain('本行业三龙头')
  expect(host.querySelector('#sector-detail')?.textContent).toContain('浦发银行')
  expect(host.querySelector('#sector-detail')?.textContent).toContain('1级行业')
})

it('keeps each column config, and the industry level control, on their own side', async () => {
  renderAt('/sector-analysis?focus=industry')
  await settle()

  expect(host.querySelector('[data-loc]')?.textContent).toBe('/sector-analysis?focus=industry')
  expect(column('industry').getAttribute('data-focused')).toBe('true')
  expect(column('concept').getAttribute('data-focused')).toBe('false')

  await act(async () => {
    host.querySelector<HTMLButtonElement>('button[title="配置概念数据源"]')?.click()
  })
  await settle()
  expect(host.textContent).not.toContain('统计层级')
  expect(host.textContent).not.toContain('一级行业')
  await act(async () => {
    ;[...host.querySelectorAll('button')].find(el => el.textContent === '取消')?.click()
  })

  const conceptConfig = localStorage.getItem('concept-analysis-config')
  await act(async () => {
    host.querySelector<HTMLButtonElement>('button[title="配置行业数据源"]')?.click()
  })
  await settle()
  expect(host.textContent).toContain('统计层级')
  await act(async () => {
    ;[...host.querySelectorAll('button')].find(el => el.textContent === '保存')?.click()
  })
  await settle()
  expect(localStorage.getItem('concept-analysis-config')).toBe(conceptConfig)
  expect(JSON.parse(localStorage.getItem('industry-analysis-config') || '{}').hierarchyLevel).toBe(1)

  await act(async () => {
    ;[...column('industry').querySelectorAll('button')].find(el => el.textContent === '3级')?.click()
  })
  await settle()
  expect(column('industry').textContent).toContain('大型')
  expect(column('industry').textContent).not.toContain('国有大行')
  expect(JSON.parse(localStorage.getItem('industry-analysis-config') || '{}').hierarchyLevel).toBe(3)
  expect(JSON.parse(localStorage.getItem('concept-analysis-config') || '{}').dimensionField).toBe('所属概念')
})

it('stacks the columns and collapses the other one when a narrow screen is focused', async () => {
  useNarrowViewport(true)
  renderAt('/sector-analysis?focus=concept')
  await settle()

  expect(column('concept').getAttribute('data-collapsed')).toBe('false')
  expect(column('industry').getAttribute('data-collapsed')).toBe('true')
  expect(column('industry').querySelector('#sector-column-body-industry')).toBeNull()

  await act(async () => {
    column('industry').querySelector<HTMLButtonElement>('[aria-controls="sector-column-body-industry"]')?.click()
  })
  await settle()
  expect(column('industry').getAttribute('data-collapsed')).toBe('false')
  expect(column('industry').textContent).toContain('热度分布')
})
