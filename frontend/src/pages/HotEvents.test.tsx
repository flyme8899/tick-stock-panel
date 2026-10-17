// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useLocation, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QK } from '@/lib/queryKeys'
import { HotEvents } from './HotEvents'

const calls = vi.hoisted(() => ({
  overview: 0,
  messages: [] as { kind: string; key: string }[],
  eventMessages: [] as string[],
}))

const hotEvent = {
  key: 'evt-1',
  name: '某某产业政策落地',
  subtitle: '来自三家的报道',
  summary: '摘要',
  heat: 91,
  category: '国内政策/宏观',
  importance: '重大',
  direction: '利好',
  red_count: 0,
  mentions: 5,
  source_count: 3,
  first_seen: '09:12',
  concepts: ['机器人'],
  stocks: [
    { key: '300124.SZ', name: '汇川技术', mentions: 3, direction: '利好' },
    { key: '002050.SZ', name: '三花智控', mentions: 2, direction: '利好' },
  ],
  etfs: [{ key: '512480.SH', name: '半导体ETF', mentions: 1, direction: '利好' }],
  sources: [],
}

vi.mock('@/lib/api', () => ({
  api: {
    newsHot: async () => ({
      candidates: [
        {
          kind: 'sector',
          key: '机器人',
          name: '机器人',
          score: 12.5,
          story_count: 6,
          effective_mentions: 6,
          sources: ['a', 'b'],
          growth: 2.4,
          baseline_effective: 1,
        },
        {
          kind: 'stock',
          key: '300124.SZ',
          name: '汇川技术',
          score: 9.1,
          story_count: 4,
          effective_mentions: 4,
          sources: ['a'],
          growth: 1.8,
          baseline_effective: 1,
        },
      ],
      events: [hotEvent],
      hint: null,
      fallback: false,
      status: 'ready',
    }),
    newsHealth: async () => ({ sources: [] }),
    newsMessages: async (kind: string, key: string) => {
      calls.messages.push({ kind, key })
      return { items: [] }
    },
    newsEventMessages: async (key: string) => {
      calls.eventMessages.push(key)
      return { items: [], stocks: hotEvent.stocks, etfs: hotEvent.etfs }
    },
    newsPush: async () => ({ configured: false, master_saved: false, master_locked: false, types: [] }),
    newsMxHot: async () => ({ enabled: false, locked: false, configured: false, used: 0, budget: 25, summary: '' }),
    newsSetSources: async () => ({}),
    newsSetPush: async () => ({}),
    newsSetMxHot: async () => ({}),
    newsPushTest: async () => ({}),
    overviewMarket: async () => {
      calls.overview += 1
      return {
        concept_rank: { leading: [{ name: '机器人' }], lagging: [] },
        industry_rank: { leading: [{ name: '银行' }], lagging: [] },
      }
    },
  },
}))

vi.mock('@/components/StockPreviewDialog', () => ({
  StockPreviewDialog: ({ symbol, name }: { symbol: string; name: string }) => (
    <div data-preview={symbol} data-preview-name={name} />
  ),
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
  calls.overview = 0
  calls.messages.length = 0
  calls.eventMessages.length = 0
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

/** 把微任务队列排空到条件成立。固定轮数在负载高时会不够, 所以按条件等。 */
async function waitFor(predicate: () => boolean, label = 'condition') {
  for (let i = 0; i < 60; i++) {
    if (predicate()) return
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
  if (!predicate()) throw new Error(`timed out waiting for ${label}`)
}

async function settle() {
  for (let i = 0; i < 12; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

function render() {
  act(() => {
    root.render(
      <MemoryRouter initialEntries={['/hot-events']}>
        <QueryClientProvider client={client}>
          <Routes>
            <Route path="/hot-events" element={<HotEvents />} />
            <Route path="/sector-analysis" element={null} />
          </Routes>
          <LocationProbe />
        </QueryClientProvider>
      </MemoryRouter>,
    )
  })
}

function switchTab(label: string) {
  return act(async () => {
    ;[...host.querySelectorAll('button')].find(el => el.textContent === label)?.click()
  })
}

function clickText(text: string) {
  return act(async () => {
    const node = [...host.querySelectorAll('button')].find(el => el.textContent?.trim() === text)
    if (!node) throw new Error(`missing clickable ${text}`)
    node.click()
  })
}

/** 事件卡片的按钮里套了很多 span，textContent 不等于标题。按包含匹配。 */
function clickContaining(text: string) {
  return act(async () => {
    const node = [...host.querySelectorAll('button')].find(el => el.textContent?.includes(text))
    if (!node) throw new Error(`missing clickable containing ${text}`)
    node.click()
  })
}

/** 点某一行候选里的「看摘录」。多行候选各有自己的按钮，得按行定位。 */
function clickExcerptFor(name: string) {
  return act(async () => {
    const row = [...host.querySelectorAll('li')].find(el => el.textContent?.includes(name))
    if (!row) throw new Error(`missing candidate row ${name}`)
    const node = [...row.querySelectorAll('button')].find(el => el.textContent?.trim() === '看摘录')
    if (!node) throw new Error(`missing excerpt button in row ${name}`)
    ;(node as HTMLButtonElement).click()
  })
}

it('opens the stock dialog from the mapped stocks on an event card', async () => {
  render()
  await settle()

  const card = host.querySelector('[data-kind], li')!
  expect(host.textContent).toContain('汇川技术')
  expect(card).toBeTruthy()

  await clickText('汇川技术')
  await waitFor(() => host.querySelector('[data-preview]') != null, 'stock preview')

  expect(host.querySelector('[data-preview]')?.getAttribute('data-preview')).toBe('300124.SZ')
  expect(host.querySelector('[data-preview-name]')?.getAttribute('data-preview-name')).toBe('汇川技术')
})

it('opens the stock dialog from the related stocks of the opened event', async () => {
  render()
  await settle()

  // 展开事件详情，读下面「相关个股 / 相关ETF」那两行。
  await clickContaining('某某产业政策落地')
  await waitFor(() => host.textContent?.includes('相关个股：') ?? false, 'event detail stocks')
  expect(calls.eventMessages).toContain('evt-1')

  await clickText('三花智控')
  await waitFor(() => host.querySelector('[data-preview]') != null, 'stock preview')
  expect(host.querySelector('[data-preview]')?.getAttribute('data-preview')).toBe('002050.SZ')
})

it('drills into the board analysis page when a sector candidate is clicked', async () => {
  render()
  await settle()
  await switchTab('板块热度')
  await settle()

  expect(host.textContent).toContain('机器人')
  await clickText('机器人')
  await waitFor(() => (host.querySelector('[data-loc]')?.textContent ?? '').includes('/sector-analysis'), 'sector navigation')

  const loc = host.querySelector('[data-loc]')?.textContent ?? ''
  expect(loc).toContain('/sector-analysis')
  expect(new URLSearchParams(loc.split('?')[1]).get('focus')).toBe('concept')
  expect(new URLSearchParams(loc.split('?')[1]).get('sector')).toBe('机器人')
})

it('still opens the stock dialog for a stock candidate, and keeps the excerpts reachable', async () => {
  render()
  await settle()
  await switchTab('热门个股')
  await settle()

  // 切到「热门个股」时会顺带给默认选中的候选拉一次摘录，这里只看新增的那次。
  calls.messages.length = 0

  await clickText('汇川技术')
  await waitFor(() => host.querySelector('[data-preview]') != null, 'stock preview')
  expect(host.querySelector('[data-preview]')?.getAttribute('data-preview')).toBe('300124.SZ')
  // 名称不再是「选中看摘录」的入口了，看摘录得还能点。
  expect(calls.messages).toEqual([])

  // 两行候选各有一个「看摘录」，要点汇川技术那一行的。
  await clickExcerptFor('汇川技术')
  await waitFor(() => calls.messages.length > 0, 'excerpt request')
  expect(calls.messages).toContainEqual({ kind: 'stock', key: '300124.SZ' })
})

it('does not fetch the board ranking until a board is actually clicked', async () => {
  render()
  await settle()
  expect(calls.overview).toBe(0)

  await switchTab('板块热度')
  await settle()
  expect(calls.overview).toBe(0)

  await clickText('机器人')
  await settle()
  expect(calls.overview).toBe(1)
})

it('sends an industry board to the industry column', async () => {
  // 「银行」只出现在行业榜里；送错列的话板块分析页找不到它。
  // 通过 client 预置「板块热度」那份缓存的响应，不动全局 mock（改了会漏到后面的用例）。
  client.setQueryData(QK.newsHot('sector'), {
    candidates: [{
      kind: 'sector',
      key: '银行',
      name: '银行',
      score: 5,
      story_count: 2,
      effective_mentions: 2,
      sources: ['a'],
      growth: 1.2,
      baseline_effective: 1,
    }],
    events: [],
    hint: null,
    fallback: false,
    status: 'ready',
  })

  render()
  await settle()
  await switchTab('板块热度')
  await settle()
  await clickText('银行')
  await settle()

  const loc = host.querySelector('[data-loc]')?.textContent ?? ''
  expect(new URLSearchParams(loc.split('?')[1]).get('focus')).toBe('industry')
  expect(new URLSearchParams(loc.split('?')[1]).get('sector')).toBe('银行')
})

const sectorEvents = {
  candidates: [],
  events: [
    {
      ...hotEvent,
      key: 'evt-sector-theme',
      name: '特朗普宣布暂缓对俄新一轮制裁',
      concepts: [],
      stocks: [],
      etfs: [],
      sector_stocks: [
        { key: '600150.SH', name: '中国船舶', source: 'sector', sector: '国防军工', sector_kind: 'theme', confidence: 0.4 },
        { key: '302132.SZ', name: '中航成飞', source: 'sector', sector: '国防军工', sector_kind: 'theme', confidence: 0.4 },
      ],
      sector_mapping: { name: '国防军工', kind: 'theme', confidence: 0.4, why: '主题词:制裁' },
      stock_source: 'sector',
    },
    {
      ...hotEvent,
      key: 'evt-sector-ind',
      name: '美国光伏电池双反终裁',
      concepts: [],
      stocks: [],
      etfs: [],
      sector_stocks: [
        { key: '300274.SZ', name: '阳光电源', source: 'sector', sector: '光伏', sector_kind: 'industry3', confidence: 0.9 },
      ],
      sector_mapping: { name: '光伏', kind: 'industry3', confidence: 0.9, why: '行业关键词:光伏' },
      stock_source: 'sector',
    },
    {
      // 有点名个股时即使带了 sector_stocks 也不显示板块推导。
      ...hotEvent,
      key: 'evt-direct',
      name: '点名个股的事件',
      sector_stocks: [
        { key: '601012.SH', name: '隆基绿能', source: 'sector', sector: '光伏', sector_kind: 'industry3', confidence: 0.9 },
      ],
      stock_source: 'direct',
    },
  ],
  hint: null,
  fallback: false,
  status: 'ready',
}

function eventCard(name: string) {
  const card = [...host.querySelectorAll('li')].find(el => el.textContent?.includes(name))
  if (!card) throw new Error(`missing event card ${name}`)
  return card
}

it('shows sector-derived stocks muted and labelled when an event names no stock', async () => {
  client.setQueryData(QK.newsHot('event'), sectorEvents)
  render()
  await settle()

  const theme = eventCard('暂缓对俄').querySelector('[data-stock-source="sector"]')
  expect(theme).toBeTruthy()
  expect(theme!.textContent).toContain('板块推导')
  expect(theme!.textContent).toContain('国防军工')
  expect(theme!.textContent).toContain('低置信')
  expect(theme!.textContent).toContain('中国船舶')
  expect(theme!.getAttribute('title')).toContain('置信度 0.4')
  expect(theme!.getAttribute('title')).toContain('主题词:制裁')
  // 推导个股压暗，不用点名个股的强调色。
  const link = [...theme!.querySelectorAll('button')].find(el => el.textContent === '中国船舶')!
  expect(link.className).toContain('text-muted')

  const ind = eventCard('光伏电池双反').querySelector('[data-stock-source="sector"]')
  expect(ind!.textContent).toContain('光伏')
  expect(ind!.textContent).not.toContain('低置信')
  expect(ind!.getAttribute('title')).toContain('申万三级')

  // 有点名个股的事件照旧，只显示点名个股。
  const direct = eventCard('点名个股的事件')
  expect(direct.querySelector('[data-stock-source="sector"]')).toBeNull()
  expect(direct.textContent).toContain('汇川技术')
  expect(direct.textContent).not.toContain('隆基绿能')

  // 推导个股照样能点开预览。
  await act(async () => link.click())
  await waitFor(() => host.querySelector('[data-preview]') != null, 'stock preview')
  expect(host.querySelector('[data-preview]')?.getAttribute('data-preview')).toBe('600150.SH')
})

it('shows sector-derived stocks in the opened event detail', async () => {
  client.setQueryData(QK.newsHot('event'), sectorEvents)
  const event = sectorEvents.events[1]
  client.setQueryData(QK.newsMessages('event', event.key), { kind: 'event', key: event.key, event, stocks: [], etfs: [], items: [] })
  render()
  await settle()

  await clickContaining('光伏电池双反')
  await waitFor(() => host.textContent?.includes('相关个股：') ?? false, 'event detail sector stocks')
  const lines = [...host.querySelectorAll('[data-stock-source="sector"]')].filter(el => el.textContent?.includes('相关个股：'))
  expect(lines).toHaveLength(1)
  expect(lines[0].textContent).toContain('板块推导')
  expect(lines[0].textContent).toContain('阳光电源')
})
