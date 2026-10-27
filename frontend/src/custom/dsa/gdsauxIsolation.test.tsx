// @vitest-environment jsdom
/**
 * P-ux 报告页「上次建议结果」钩 —— 隔离与契约测（批次 20261026gdsaux）。
 *
 * 仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
 *
 * 这里钉死的是「不能假装」的几条：
 * - 数据只来自 `/api/dsa/ledger-audit`，且按股带 `code`（N1）
 * - 「上次」= 该股最新 1 条（N2），展开合计 ≤3
 * - 不可达 / 无行 → 诚实空态，不拿样例数或硬编码结算行填充
 * - 徽章恒在；不印单股胜率%
 * - `entry_next_open` 空就是「未注入」，前端不倒推开盘价
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LastAdviceResultCard, TSP_AUDIT_BADGE, latestAdviceRows } from './DecisionWorkspace'
import type { DsaLedgerAudit, DsaLedgerAuditRow } from './client'

const SOURCE = readFileSync(resolve(__dirname, 'DecisionWorkspace.tsx'), 'utf8')

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

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
    advice_generated_at: null,
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
      rows_total: 9,
      denominator: 5,
      wins: 3,
      losses: 2,
      excluded_from_denominator: 2,
      unfillable_count: 1,
      entry_basis: ['close 与 next_open 并列'],
      win_rate_pct: 60,
      sample_below_threshold: false,
      min_sample_for_percent: 30,
      unavailable_reason: '',
      argument_quality_denominator: 0,
      argument_quality_excluded_late: 0,
      argument_quality_excluded_missing_snapshot: 0,
      argument_quality_suppressed: true,
      argument_quality_pct: null,
    },
    rows: [],
    sources: {},
    ...over,
  }
}

/** 装一个 fake fetch：记下每次请求的 URL，回给定 payload。 */
function stubFetch(payload: DsaLedgerAudit) {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    calls.push(String(input))
    // client.parse() 看 content-type 决定走 json 还是 text，桩必须给头
    return {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    } as unknown as Response
  }))
  return calls
}

async function render(code: string) {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <LastAdviceResultCard code={code} />
      </QueryClientProvider>,
    )
  })
  // 等 useQuery 的 promise 链真正落地：轮询到卡片离开「正在读取」态
  for (let i = 0; i < 20; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    const text = host.textContent ?? ''
    if (!text.includes('正在读取该股账本结算记录')) return
  }
}

describe('N1 · client 按股过滤', () => {
  it('把 code 打进 query，并保留 limit', async () => {
    const calls = stubFetch(ledger())
    await render('600519')
    expect(calls.length).toBeGreaterThan(0)
    const url = calls[0]
    expect(url).toContain('/api/dsa/ledger-audit?')
    expect(url).toContain('code=600519')
    expect(url).toContain('limit=')
  })

  it('空代码不发请求（enabled=false），不会打出一个 code= 空的请求', async () => {
    const calls = stubFetch(ledger())
    await render('   ')
    expect(calls).toEqual([])
  })
})

describe('N2 · 「上次」= 该股最新 1 条', () => {
  it('按 analysis_history_id 降序，只留主条', () => {
    const rows = [
      row({ analysis_history_id: 7 }),
      row({ analysis_history_id: 12 }),
      row({ analysis_history_id: 9 }),
    ]
    expect(latestAdviceRows(rows, '600519').map(r => r.analysis_history_id)).toEqual([12, 9, 7])
  })

  it('同 id 时后出现的算更新', () => {
    const rows = [
      row({ analysis_history_id: 5, outcome: 'window_old' }),
      row({ analysis_history_id: 5, outcome: 'newer' }),
    ]
    expect(latestAdviceRows(rows, '600519')[0].outcome).toBe('newer')
  })

  it('丢掉别的股票（不信任上游回退到全量）', () => {
    const rows = [
      row({ analysis_history_id: 3, code: '600519' }),
      row({ analysis_history_id: 99, code: '000001' }),
    ]
    const kept = latestAdviceRows(rows, '600519')
    expect(kept).toHaveLength(1)
    expect(kept[0].code).toBe('600519')
  })

  it('默认只渲染主条，展开后合计 ≤3', async () => {
    const rows = [1, 2, 3, 4, 5].map(id => row({ analysis_history_id: id }))
    stubFetch(ledger({ rows }))
    await render('600519')

    const primaries = () => host.querySelectorAll('[data-last-advice-row]')
    expect(primaries()).toHaveLength(1)
    expect(host.querySelector('[data-last-advice-primary="true"]')).not.toBeNull()
    // 主条必须是最新那条（id=5）
    expect(host.querySelector('[data-last-advice-primary="true"]')?.textContent).toContain('600519')
    expect(latestAdviceRows(rows, '600519')[0].analysis_history_id).toBe(5)

    const toggle = host.querySelector('[data-last-advice-toggle]') as HTMLButtonElement
    expect(toggle).not.toBeNull()
    await act(async () => { toggle.click() })
    expect(primaries()).toHaveLength(3)
    expect(primaries().length).toBeLessThanOrEqual(3)

    // 再点回去收起
    await act(async () => { toggle.click() })
    expect(primaries()).toHaveLength(1)
  })

  it('只有 1 条时不给「展开更多」按钮', async () => {
    stubFetch(ledger({ rows: [row({ analysis_history_id: 2 })] }))
    await render('600519')
    expect(host.querySelector('[data-last-advice-toggle]')).toBeNull()
  })
})

describe('G-T5 · 诚实空态', () => {
  it('available=false 时带出 unavailable_reason，且不出现任何结算行', async () => {
    stubFetch(ledger({
      available: false,
      summary: { ...ledger().summary, unavailable_reason: 'sidecar 未启动' },
      rows: [row({ analysis_history_id: 1 })],
    }))
    await render('600519')

    const text = host.textContent ?? ''
    expect(text).toContain('账本暂不可读')
    expect(text).toContain('sidecar 未启动')
    expect(text).toContain('不会用模拟数据填充')
    // 即使 payload 里塞了行，不可达时也不许渲染
    expect(host.querySelectorAll('[data-last-advice-row]')).toHaveLength(0)
  })

  it('可读但该股无结算行 → 诚实空态', async () => {
    stubFetch(ledger({ rows: [row({ analysis_history_id: 1, code: '000001' })] }))
    await render('600519')

    const text = host.textContent ?? ''
    expect(text).toContain('该股暂无账本结算记录')
    expect(host.querySelectorAll('[data-last-advice-row]')).toHaveLength(0)
  })

  it('请求失败 → 空态而非抛墙', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 503,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({}),
      text: async () => '{}',
    } as unknown as Response)))
    await render('600519')
    expect(host.textContent ?? '').toContain('账本暂不可读')
  })
})

describe('G-T6 · 徽章 / 红涨绿跌 / 成本脚注', () => {
  it('主条带「未经 TSP 审计」徽章', async () => {
    stubFetch(ledger({ rows: [row({ analysis_history_id: 3 })] }))
    await render('600519')
    expect(TSP_AUDIT_BADGE).toBe('未经 TSP 审计')
    expect(host.querySelector('[data-tsp-audit-badge]')?.textContent).toBe('未经 TSP 审计')
  })

  it('展示收益时并排写成本来回 18.2bp', async () => {
    stubFetch(ledger({ rows: [row({ analysis_history_id: 3, simulated_return_pct: 2.4 })] }))
    await render('600519')
    const ret = host.querySelector('[data-last-advice-return]')
    expect(ret?.textContent).toContain('模拟收益')
    expect(ret?.textContent).toContain('18.2bp')
  })

  it('无收益字段时不硬造一行收益文案', async () => {
    stubFetch(ledger({ rows: [row({ analysis_history_id: 3, simulated_return_pct: null })] }))
    await render('600519')
    expect(host.querySelector('[data-last-advice-return]')).toBeNull()
  })

  it('赢用红、亏用绿（红涨绿跌）', () => {
    // 走 priceColorClass 的既有口径：正数红、负数绿
    expect(SOURCE).toContain("priceColorClass(row.outcome === 'win' ? 1 : row.outcome === 'loss' ? -1 : 0)")
  })

  it('单股卡不印胜率百分比', async () => {
    stubFetch(ledger({ rows: [row({ analysis_history_id: 3 })] }))
    await render('600519')
    // summary.win_rate_pct=60，但本卡不该把全账本胜率搬成「该股胜率」
    const text = host.textContent ?? ''
    expect(text).not.toContain('60.00%')
    expect(text).not.toContain('60%')
    // 提示语里出现「胜率」是在说明「不套全账本胜率」，不含百分比即不算违规
    expect(text).not.toMatch(/胜率[：:]?\s*\d/)
  })
})

describe('N3 · entry_next_open 照实', () => {
  it('null 显示「未注入」', async () => {
    stubFetch(ledger({ rows: [row({ analysis_history_id: 3, entry_next_open: null })] }))
    await render('600519')
    expect(host.querySelector('[data-last-advice-next-open]')?.textContent).toBe('未注入')
  })

  it('不倒推开盘价：源码里没有用收盘价/跳空重算 next_open', () => {
    const idx = SOURCE.indexOf('export function LastAdviceResultCard')
    const body = SOURCE.slice(idx, idx + 6000)
    expect(body).toContain("row.entry_next_open ?? '未注入'")
    // 危险写法：拿 entry_close 乘/加 entry_gap_pct 造出开盘价
    expect(body).not.toMatch(/entry_next_open\s*\?\?[^)]*entry_close/)
  })
})

describe('不可成交 / 分母态照实', () => {
  it('unfillable 标「不可成交 · 不计入分母」并列原因', async () => {
    stubFetch(ledger({
      rows: [row({ analysis_history_id: 4, outcome: '', unfillable: 'limit_up_one_word', in_denominator: false })],
    }))
    await render('600519')
    expect(host.querySelector('[data-last-advice-denominator]')?.textContent).toBe('不可成交 · 不计入分母')
    expect(host.textContent ?? '').toContain('limit_up_one_word')
  })
})

describe('G-T3 · 数据源唯一，无旁路写', () => {
  it('只打 ledger-audit，不碰 backtest/run 与 quant-evidence 写入', async () => {
    const calls = stubFetch(ledger({ rows: [row({ analysis_history_id: 3 })] }))
    await render('600519')
    for (const url of calls) {
      expect(url).toContain('/api/dsa/ledger-audit')
      expect(url).not.toContain('backtest/run')
    }
  })

  it('组件源码不调 backtest/run、不硬编码结算行', () => {
    const idx = SOURCE.indexOf('export function LastAdviceResultCard')
    const body = SOURCE.slice(idx, idx + 6000)
    expect(body).not.toContain('backtest/run')
    expect(body).not.toContain('SAMPLE_REPORT')
    // 不许用样例数顶替
    expect(body).not.toContain('sample ?')
  })
})
