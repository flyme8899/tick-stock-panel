// @vitest-environment jsdom
//
// P-arg-snap 前端隔离测：论据快照最小行 + late 标记 + null≠false 文案。
//
// 守的是**渲染诚实**，不是后端判定（那在 test_dsa_ledger_arg_snap.py）：
// 后端已经把 null 和 false 分开了，前端把它们渲染成同一句话等于白分。
//
// 仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { LastAdviceResultCard, LedgerAuditPanel, TSP_AUDIT_BADGE } from './DecisionWorkspace'
import type { DsaLedgerAudit, DsaLedgerAuditRow } from './client'

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  client.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** fake fetch：client.parse() 看 content-type 决定解析方式，桩必须给头。 */
function stubFetch(payload: DsaLedgerAudit) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    } as unknown as Response)),
  )
}

const ADVICE = '2026-10-27T09:30:00+08:00'

function row(over: Partial<DsaLedgerAuditRow> & { analysis_history_id: number }): DsaLedgerAuditRow {
  return {
    code: '600519',
    outcome: 'win',
    entry_close: 100,
    entry_next_open: 101,
    entry_gap_pct: 1,
    entry_basis: 'close 与 next_open 并列',
    unfillable: null,
    in_denominator: true,
    source: 'analysis_bridge',
    source_note: '自动',
    simulated_return_pct: 1.5,
    bull_case: null,
    bear_case: null,
    risk_veto: null,
    snapshot_asof: null,
    advice_generated_at: ADVICE,
    snapshot_late: false,
    in_argument_quality_denominator: false,
    ...over,
  }
}

function ledger(over: Partial<DsaLedgerAudit> = {}): DsaLedgerAudit {
  return {
    available: true,
    summary: {
      audit_status: 'unaudited',
      badge: TSP_AUDIT_BADGE,
      rows_total: 1,
      denominator: 1,
      wins: 1,
      losses: 0,
      excluded_from_denominator: 0,
      unfillable_count: 0,
      entry_basis: ['close 与 next_open 并列'],
      win_rate_pct: null,
      sample_below_threshold: true,
      min_sample_for_percent: 30,
      unavailable_reason: '',
      argument_quality_denominator: 1,
      argument_quality_excluded_late: 1,
      argument_quality_excluded_missing_snapshot: 2,
      argument_quality_suppressed: true,
      argument_quality_pct: null,
    },
    rows: [],
    sources: {},
    ...over,
  }
}

async function render(node: React.ReactElement) {
  await act(async () => {
    root.render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
  })
}

/** 渲染「上次建议结果」卡片，并等 useQuery 落地（离开读取中态）。 */
async function renderCard(payload: DsaLedgerAudit, code = '600519') {
  stubFetch(payload)
  await render(<LastAdviceResultCard code={code} />)
  for (let i = 0; i < 30; i++) {
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0))
    })
    const text = host.textContent ?? ''
    if (!text.includes('正在读取该股账本结算记录')) return
  }
}

// ───────────────────────────────────── null ≠ false：文案必须分开

it('renders "no risk conclusion" for a null veto, not "not vetoed"', async () => {
  await renderCard(ledger({ rows: [row({ analysis_history_id: 1, risk_veto: null })] }))
  const el = host.querySelector('[data-arg-snap-veto]')
  expect(el?.textContent).toBe('无风控结论')
  expect(el?.textContent).not.toContain('未否决')
})

it('renders "not vetoed" only for an explicit {veto:false} object', async () => {
  await renderCard(ledger({ rows: [row({ analysis_history_id: 1, risk_veto: { veto: false, reason: null } })] }))
  const el = host.querySelector('[data-arg-snap-veto]')
  expect(el?.textContent).toBe('未否决')
  expect(el?.textContent).not.toContain('无风控结论')
})

it('renders the reason when the veto fired', async () => {
  await renderCard(
    ledger({ rows: [row({ analysis_history_id: 1, risk_veto: { veto: true, reason: '单票集中度超限' } })] }),
  )
  const el = host.querySelector('[data-arg-snap-veto]')
  expect(el?.textContent).toContain('已否决')
  expect(el?.textContent).toContain('单票集中度超限')
})

// ───────────────────────────────────── late 标记可见

it('marks a late snapshot visibly', async () => {
  await renderCard(
    ledger({
      rows: [
        row({ analysis_history_id: 1, snapshot_asof: '2026-10-27T09:30:01+08:00', snapshot_late: true }),
      ],
    }),
  )
  const badge = host.querySelector('[data-arg-snap-late]')
  expect(badge).not.toBeNull()
  expect(badge?.textContent).toContain('迟到')
  expect(badge?.textContent).toContain('不计入论据质量')
})

it('does not mark a non-late snapshot', async () => {
  await renderCard(ledger({ rows: [row({ analysis_history_id: 1, snapshot_late: false })] }))
  expect(host.querySelector('[data-arg-snap-late]')).toBeNull()
})

// ───────────────────────────────────── 空态诚实、禁 fabricate

it('shows honest placeholders when the upstream gave nothing', async () => {
  await renderCard(ledger({ rows: [row({ analysis_history_id: 1 })] }))
  expect(host.querySelector('[data-arg-snap-bull]')?.textContent).toBe('无多头论据')
  expect(host.querySelector('[data-arg-snap-bear]')?.textContent).toBe('无空头论据')
  expect(host.querySelector('[data-arg-snap-asof]')?.textContent).toBe('未记录')
  expect(host.querySelector('[data-arg-snap-empty]')).not.toBeNull()
})

it('never invents a snapshot time when the field is null', async () => {
  await renderCard(ledger({ rows: [row({ analysis_history_id: 1, snapshot_asof: null })] }))
  const asof = host.querySelector('[data-arg-snap-asof]')?.textContent ?? ''
  expect(asof).toBe('未记录')
  // 不许出现任何日期/时间样式的字符串顶替
  expect(asof).not.toMatch(/\d{4}-\d{2}-\d{2}/)
})

// ───────────────────────────────────── 论据内容如实呈现

it('renders bull and bear case text', async () => {
  await renderCard(
    ledger({ rows: [row({ analysis_history_id: 1, bull_case: '订单拐点', bear_case: '估值偏高' })] }),
  )
  expect(host.querySelector('[data-arg-snap-bull]')?.textContent).toContain('订单拐点')
  expect(host.querySelector('[data-arg-snap-bear]')?.textContent).toContain('估值偏高')
})

it('truncates a long case but keeps the full text in title', async () => {
  const long = '甲'.repeat(200)
  await renderCard(ledger({ rows: [row({ analysis_history_id: 1, bull_case: long })] }))
  const el = host.querySelector('[data-arg-snap-bull]')
  expect(el?.textContent?.length).toBeLessThan(long.length)
  expect(el?.getAttribute('title')).toBe(long)
})

it('shows both timestamps for cross-checking', async () => {
  await renderCard(
    ledger({
      rows: [
        row({
          analysis_history_id: 1,
          snapshot_asof: '2026-10-27T09:00:00+08:00',
          advice_generated_at: ADVICE,
        }),
      ],
    }),
  )
  expect(host.querySelector('[data-arg-snap-asof]')?.textContent).toBe('2026-10-27T09:00:00+08:00')
  expect(host.querySelector('[data-arg-snap-advice]')?.textContent).toBe(ADVICE)
})

// ───────────────────────────────────── 面板：质量分母与胜率分账

it('shows the argument-quality denominator separately from the win rate', async () => {
  await render(<LedgerAuditPanel data={ledger()} />)
  const el = host.querySelector('[data-ledger-arg-quality]')
  expect(el).not.toBeNull()
  expect(el?.textContent).toContain('论据质量分母 1')
  expect(el?.textContent).toContain('不含快照迟到 1')
  expect(el?.textContent).toContain('缺快照 2')
  expect(el?.textContent).toContain('本拍不给质量百分比')
})

it('keeps the unaudited badge on the panel', async () => {
  await render(<LedgerAuditPanel data={ledger()} />)
  expect(host.querySelector('[data-tsp-audit-badge]')?.textContent).toBe(TSP_AUDIT_BADGE)
})

it('never renders a fabricated quality percentage', async () => {
  await render(<LedgerAuditPanel data={ledger()} />)
  const text = host.textContent ?? ''
  expect(text).not.toMatch(/论据质量\s*[:：]?\s*\d+(\.\d+)?%/)
})
