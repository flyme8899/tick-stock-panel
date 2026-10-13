// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import { SectorAnalysis } from './SectorAnalysis'

const calls = vi.hoisted(() => ({
  rows: [] as string[],
  rotation: [] as { kind: string; level?: number }[],
  // 置 true 时 ext_gn_ths 不再返回「银行概念」，用来模拟深链板块在刷新后短暂消失。
  dropBankConcept: false,
  // 置 true 时 ext_gn_ths 整列为空，模拟某一列数据源未接入/为空。
  emptyConcept: false,
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
          // 名字和描述都更像行业, 但字段是概念。旧的关键词打分会把它选成行业源。
          id: 'board_mix',
          label: '行业板块',
          description: '行业 sector 申万',
          fields: [
            { name: 'symbol', dtype: 'str', label: '代码' },
            { name: '所属概念', dtype: 'str', label: '概念 sector' },
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
      calls.rows.push(id)
      if (id === 'ext_gn_ths') {
        return {
          id,
          total: 2,
          fields: [
            { name: 'symbol', dtype: 'str', label: '代码' },
            { name: '所属概念', dtype: 'str', label: '所属概念' },
          ],
          rows: (calls.emptyConcept ? [] : [
            { symbol: '000001.SZ', name: '平安银行', 所属概念: '银行概念;融资融券;银行' },
            { symbol: '000002.SZ', name: '万科A', 所属概念: '地产概念' },
          ]).filter(row => !(calls.dropBankConcept && row.所属概念.includes('银行概念'))),
        }
      }
      if (id === 'board_mix') {
        return {
          id,
          total: 3,
          fields: [
            { name: 'symbol', dtype: 'str', label: '代码' },
            { name: '所属概念', dtype: 'str', label: '概念 sector' },
          ],
          rows: [
            { symbol: '000001.SZ', name: '平安银行', 所属概念: '融资融券' },
            { symbol: '600000.SH', name: '浦发银行', 所属概念: '深股通' },
            { symbol: '000002.SZ', name: '万科A', 所属概念: '短剧游戏' },
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
    sectorRotation: async (params: { kind: string; level?: number }) => {
      calls.rotation.push({ kind: params.kind, level: params.level })
      const industryName = params.level === 3 ? '大型' : params.level === 2 ? '国有大行' : '银行'
      const name = params.kind === 'industry' ? industryName : '银行概念'
      return {
        status: 'ok',
        date: '2026-10-10',
        as_of: '15:00',
        timeline: [{
          time: '15:00',
          rotation: params.kind === 'industry' ? 0.31 : 0.42,
          leader: name,
          leader_pct: 0.02,
          market_pct: 0.01,
        }],
        sectors: [
          {
            name: '融资融券',
            pct_now: 0.05,
            pct_prev: 0.01,
            rank_now: 1,
            rank_prev: 1,
            rank_change: 0,
            flow: null,
            score: 2,
            n_members: 2,
            n_members_with_bars: 2,
          },
          {
            name,
            pct_now: 0.02,
            pct_prev: 0.01,
            rank_now: 2,
            rank_prev: 2,
            rank_change: 1,
            flow: null,
            score: 1,
            n_members: 2,
            n_members_with_bars: 2,
          },
        ],
      }
    },
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
  calls.rows.length = 0
  calls.rotation.length = 0
  calls.dropBankConcept = false
  calls.emptyConcept = false
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

/** 按条件等待, 不用固定轮数 —— 负载高时固定轮数会不够。 */
async function waitFor(predicate: () => boolean, label = 'condition') {
  for (let i = 0; i < 60; i++) {
    if (predicate()) return
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
  if (!predicate()) throw new Error(`timed out waiting for ${label}`)
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

function sectorKeys(kind: string) {
  return [...column(kind).querySelectorAll('[data-sector]')].map(node => node.getAttribute('data-sector'))
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
  expect(column('concept').querySelector('[data-rotation="concept"]')?.textContent).toContain('银行概念')
  expect(column('concept').querySelector('[data-rotation="concept"]')?.textContent).not.toContain('融资融券')
  expect(column('industry').querySelector('[data-rotation="industry"]')?.textContent).not.toContain('融资融券')
  expect(column('concept').textContent).not.toContain('热度分布')
  expect(column('concept').querySelector('[aria-label="行业层级"]')).toBeNull()
  expect(column('concept').getAttribute('data-source')).toBe('ext_gn_ths')

  expect(column('industry').textContent).toContain('1级行业')
  expect(column('industry').textContent).toContain('2 个')
  expect(column('industry').textContent).toContain('热度分布')
  expect(column('industry').textContent).toContain('银行')
  expect(column('industry').textContent).toContain('行业轮动')
  expect(column('industry').textContent).not.toContain('国有大行')
  expect(column('industry').textContent).not.toContain('银行概念')
  expect(column('industry').getAttribute('data-source')).toBe('ext_hy_ths')
  expect(new Set(sectorKeys('concept'))).toEqual(new Set(['银行概念', '地产概念', '银行']))
  expect(new Set(sectorKeys('industry'))).toEqual(new Set(['银行', '房地产']))
  // 「银行」两列都有 —— 概念/行业重名是真实存在的, 深链判列必须据此优先 focus
  // 而不是无条件概念列（见下面的同名深链用例）。除它之外两列不重叠。
  expect(sectorKeys('concept').filter(key => key !== '银行').some(key => sectorKeys('industry').includes(key))).toBe(false)
  expect(sectorKeys('industry')).not.toContain('融资融券')
  expect(sectorKeys('concept')).not.toContain('融资融券')
  expect(calls.rows).toContain('ext_gn_ths')
  expect(calls.rows).toContain('ext_hy_ths')
  expect(calls.rows).not.toContain('board_mix')
  expect(calls.rotation).toContainEqual({ kind: 'industry', level: 1 })
  expect(calls.rotation).toContainEqual({ kind: 'concept', level: undefined })
  expect(QK.sectorBoardRows('industry', 'ext_hy_ths', 12000)).not.toEqual(QK.sectorBoardRows('concept', 'ext_gn_ths', 12000))
  expect(QK.sectorRotation('industry', undefined, 5, '', 'summary', 1)).not.toEqual(
    QK.sectorRotation('industry', undefined, 5, '', 'summary', 3),
  )
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

it('selects the board named in the url, on the column the url points at', async () => {
  // 热门事件点板块直达：?focus=<kind>&sector=<板块名>。
  renderAt('/sector-analysis?focus=concept&sector=%E9%93%B6%E8%A1%8C%E6%A6%82%E5%BF%B5')
  await waitFor(() => host.querySelector('#sector-detail')?.getAttribute('data-detail-kind') === 'concept', 'deep-linked board')

  const detail = host.querySelector('#sector-detail')
  expect(detail?.getAttribute('data-detail-kind')).toBe('concept')
  expect(detail?.textContent).toContain('银行概念')
  expect(detail?.textContent).toContain('平安银行')
  expect(detail?.textContent).not.toContain('点击上方概念或行业')
})

it('honours the focused column when the board name exists in both', async () => {
  // 概念与行业重名（「银行」两列都有）。focus=industry 的深链必须落行业列,
  // 不能因为「概念列也有这个名字」就被抢到概念列。
  renderAt('/sector-analysis?focus=industry&sector=%E9%93%B6%E8%A1%8C')
  await waitFor(
    () => (host.querySelector('[data-kind="industry"]')?.querySelectorAll('[data-sector]').length ?? 0) > 0,
    'industry rows',
  )
  await waitFor(
    () => host.querySelector('#sector-detail')?.getAttribute('data-detail-kind') === 'industry',
    'focused column wins',
  )
  expect(host.querySelector('#sector-detail')?.getAttribute('data-detail-kind')).toBe('industry')
  expect(host.querySelector('#sector-detail')?.textContent).toContain('本行业三龙头')
})

it('corrects the column when the deep-linked board lives in the other one', async () => {
  // 上游判列只能看涨跌榜头尾, 排在中间的行业板块会被兜底成概念列。
  // 板块分析页两列完整 stats 都在手边, 要据此纠偏到真正含该板块的那一列。
  // 「房地产」只存在于行业列（概念列是「地产概念」）, 但 URL 说 focus=concept。
  renderAt('/sector-analysis?focus=concept&sector=%E6%88%BF%E5%9C%B0%E4%BA%A7')
  // 等两列 stats 都就绪（行业列出现板块行）—— 纠偏要等到那时候才知道房地产在行业列。
  await waitFor(
    () => (host.querySelector('[data-kind="industry"]')?.querySelectorAll('[data-sector]').length ?? 0) > 0,
    'industry rows',
  )
  await waitFor(
    () => host.querySelector('#sector-detail')?.getAttribute('data-detail-kind') === 'industry',
    'industry correction',
  )

  const detail = host.querySelector('#sector-detail')
  // 必须落到行业列, 而不是停在概念列显示「当前列表里没有这个概念」。
  expect(detail?.getAttribute('data-detail-kind')).toBe('industry')
  expect(detail?.textContent).toContain('房地产')
  expect(detail?.textContent).not.toContain('当前列表里没有这个概念')
})

it('waits for the board list before selecting, and does not claim the board is missing', async () => {
  // 深链指向一个不存在的板块：要停在提示上, 而不是先选中、渲染出
  // 「当前列表里没有这个概念」再跳走。等两列都就绪后给一句明确的「未找到」。
  renderAt('/sector-analysis?focus=industry&sector=%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E6%9D%BF%E5%9D%97')
  // 等板块列表就绪（列里出现板块行）再断言没被选中。
  // 用裸选择器轮询 —— column() 找不到节点会抛, 不能放进条件里。
  await waitFor(
    () => (host.querySelector('[data-kind="industry"]')?.querySelectorAll('[data-sector]').length ?? 0) > 0,
    'industry rows',
  )

  const detail = host.querySelector('#sector-detail')
  expect(detail?.getAttribute('data-detail-kind')).toBeNull()
  expect(detail?.textContent).toContain('未找到板块「不存在的板块」')
  expect(detail?.textContent).not.toContain('当前列表里没有这个概念')
})

it('still reports a missing deep-linked board when one column has no data at all', async () => {
  // 概念列数据源为空(未接入/加载失败)时, 它永远不会有数据。
  // 「未找到」的判定要以两列**加载完成**为准, 不能要求两列都有数据 ——
  // 否则这种情况下提示永远不出现, 又回到点了没反应的静默。
  calls.emptyConcept = true
  renderAt('/sector-analysis?focus=concept&sector=%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E6%9D%BF%E5%9D%97')
  await waitFor(
    () => (host.querySelector('#sector-detail')?.textContent ?? '').includes('未找到板块'),
    'missing hint despite empty column',
  )
  expect(host.querySelector('#sector-detail')?.textContent).toContain('未找到板块「不存在的板块」')
})

it('applies a deep link only once, so a refetch cannot steal a manual pick', async () => {
  // URL 里的 sector 会一直留着。如果深链 effect 每次 stats 变化都重新应用,
  // 那么「深链板块在刷新后短暂消失再回来」就会把用户手动点的另一个板块抢回去。
  renderAt('/sector-analysis?focus=concept&sector=%E9%93%B6%E8%A1%8C%E6%A6%82%E5%BF%B5')
  await waitFor(() => host.querySelector('#sector-detail')?.getAttribute('data-detail-kind') === 'concept', 'deep-linked board')

  // 用户手动改选另一个概念板块。
  await act(async () => {
    column('concept').querySelector<HTMLButtonElement>('[data-sector="地产概念"]')?.click()
  })
  await settle()
  expect(host.querySelector('#sector-detail')?.textContent).toContain('地产概念')

  // 模拟刷新：深链板块「银行概念」先消失, 再出现 —— deepResolved 走了 false→true。
  calls.dropBankConcept = true
  await act(async () => { await client.invalidateQueries() })
  await settle()
  calls.dropBankConcept = false
  await act(async () => { await client.invalidateQueries() })
  await settle()

  // 用户的手动选择必须还在, 不能被深链抢回「银行概念」。
  const detail = host.querySelector('#sector-detail')
  expect(detail?.textContent).toContain('地产概念')
  expect(detail?.textContent).not.toContain('银行概念')
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
  expect(column('industry').textContent).toContain('3级行业')
  expect(new Set(sectorKeys('industry'))).toEqual(new Set(['大型', '中型', '全国']))
  expect(column('industry').textContent).toContain('大型')
  expect(column('industry').textContent).not.toContain('国有大行')
  expect(column('industry').textContent).not.toContain('融资融券')
  expect(calls.rotation).toContainEqual({ kind: 'industry', level: 3 })
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
  expect(new Set(sectorKeys('industry'))).toEqual(new Set(['银行', '房地产']))
  expect(sectorKeys('industry')).not.toContain('融资融券')
})

it('keeps industry rows when the saved industry source points at the concept preset', async () => {
  localStorage.setItem('industry-analysis-config', JSON.stringify({
    hierarchyLevel: 1,
    configId: 'ext_gn_ths',
    dimensionField: '所属概念',
  }))
  renderAt('/sector-analysis')
  await settle()

  expect(column('industry').getAttribute('data-source')).toBe('ext_hy_ths')
  expect(new Set(sectorKeys('industry'))).toEqual(new Set(['银行', '房地产']))
  expect(new Set(sectorKeys('concept'))).toEqual(new Set(['银行概念', '地产概念', '银行']))
  expect(calls.rows).not.toContain('board_mix')
})

it('shows turnover on the amount tab and the leader name on the leader tab', async () => {
  renderAt('/sector-analysis')
  await settle()

  await act(async () => {
    ;[...column('industry').querySelectorAll('button')].find(el => el.textContent === '成交')?.click()
  })
  await settle()
  const industryAmounts = [...column('industry').querySelectorAll('[data-metric="amount"]')].map(node => node.textContent)
  expect(industryAmounts).toContain('18.00亿')
  expect(industryAmounts).toContain('5.00亿')
  expect(column('industry').textContent).toContain('+3.00%')

  await act(async () => {
    ;[...column('concept').querySelectorAll('button')].find(el => el.textContent === '成交')?.click()
  })
  await settle()
  expect(column('concept').querySelector('[data-metric="amount"]')?.textContent).toMatch(/亿|万/)

  await act(async () => {
    ;[...column('industry').querySelectorAll('button')].find(el => el.textContent === '龙头')?.click()
  })
  await settle()
  const leaders = [...column('industry').querySelectorAll('[data-metric="leader"]')].map(node => node.textContent)
  expect(leaders).toContain('平安银行')
  expect(leaders).toContain('万科A')
  expect(column('industry').textContent).toContain('+2.00%')
})
