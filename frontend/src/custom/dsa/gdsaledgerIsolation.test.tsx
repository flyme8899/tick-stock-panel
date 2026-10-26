// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { LedgerAuditPanel, TSP_AUDIT_BADGE, QuantEvidencePanel } from './DecisionWorkspace'
import type { DsaLedgerAudit, DsaQuantEvidence } from './client'

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

const ledger: DsaLedgerAudit = {
  available: true,
  summary: {
    audit_status: 'unaudited',
    badge: '未经 TSP 审计',
    rows_total: 4,
    denominator: 2,
    wins: 1,
    losses: 1,
    excluded_from_denominator: 2,
    unfillable_count: 1,
    entry_basis: ['close 与 next_open 并列', 'close（上游原值·漏隔夜跳空）'],
    win_rate_pct: null,
    sample_below_threshold: true,
    min_sample_for_percent: 30,
    unavailable_reason: '',
  },
  rows: [
    { analysis_history_id: 11, code: '600000', outcome: 'win', entry_close: 10, entry_next_open: 10.35, entry_gap_pct: 3.5, entry_basis: 'close 与 next_open 并列', unfillable: null, in_denominator: true, source: 'analysis_bridge', source_note: '自动', simulated_return_pct: 3.2 },
    { analysis_history_id: 15, code: '300750', outcome: 'win', entry_close: 11, entry_next_open: null, entry_gap_pct: null, entry_basis: 'close（上游原值·漏隔夜跳空）', unfillable: 'limit_up_one_word', in_denominator: false, source: 'analysis_bridge', source_note: '自动', simulated_return_pct: 0 },
  ],
  sources: {},
  audit_items: [
    { id: 'A1', title: '入场价并列', state: 'partial', detail: '' },
    { id: 'A7', title: '未审计标', state: 'shown', detail: '' },
  ],
}

it('shows the audit badge and refuses to print a win rate on a small sample', async () => {
  await act(async () => {
    root.render(<LedgerAuditPanel data={ledger} />)
  })

  expect(TSP_AUDIT_BADGE).toBe('未经 TSP 审计')
  const badge = host.querySelector('[data-tsp-audit-badge]')
  expect(badge?.textContent).toBe('未经 TSP 审计')
  expect(host.textContent).toContain('样本不足')
  expect(host.textContent).not.toMatch(/胜率 \d/)
  // 不可成交的行要看得见，不能悄悄消失
  expect(host.textContent).toContain('不可成交')
})

it('keeps the badge when the ledger is unavailable', async () => {
  await act(async () => {
    root.render(
      <LedgerAuditPanel
        data={{ ...ledger, available: false, rows: [], summary: { ...ledger.summary, unavailable_reason: 'sidecar 不可达' } }}
      />,
    )
  })
  expect(host.textContent).toContain('未经 TSP 审计')
  expect(host.textContent).toContain('sidecar 不可达')
})

const evidence = (patch: Partial<DsaQuantEvidence>): DsaQuantEvidence => ({
  available: true,
  meta: {
    source: 'tsp',
    report: 'sample',
    period: '2026-07-08 ~ 2026-10-08',
    universe: 5200,
    benchmark_return: '-4.00%',
    updated: '2026-10-08',
    as_of: '2026-10-08',
    env_label: '弱势下行',
    freshness: { as_of: '2026-10-08', expires_on: '2027-01-06', age_days: 18, expiry_days: 90, expired: false, reason: '' },
  },
  caveat: '机械回测，不是本策略的实盘业绩。',
  skills: [{ name: 'ma_golden_cross', matched: ['MA金叉'], returns: [-14.7], sample_sizes: [59], verdict: '', note: '', guidance: '' }],
  ...patch,
})

it('flags an expired evidence set', async () => {
  await act(async () => {
    root.render(<QuantEvidencePanel data={evidence({ expired: true })} defaultOpen />)
  })
  expect(host.querySelector('[data-quant-expired]')).not.toBeNull()
})

it('flags a sample below the guidance threshold', async () => {
  await act(async () => {
    root.render(<QuantEvidencePanel data={evidence({ sample_below_threshold: true, min_sample_for_guidance: 100 })} defaultOpen />)
  })
  expect(host.querySelector('[data-quant-sample-below]')).not.toBeNull()
  expect(host.textContent).toContain('100')
})

it('keeps the audit badge string in sync with the backend contract', () => {
  const backend = readFileSync(resolve(__dirname, '../../../../backend/app/custom/dsa/ledger_audit.py'), 'utf8')
  const match = backend.match(/TSP_AUDIT_BADGE:\s*str\s*=\s*"([^"]+)"/)
  expect(match?.[1]).toBe(TSP_AUDIT_BADGE)
})

it('never renders an audit-passed state from the frontend side', () => {
  const workspace = readFileSync(resolve(__dirname, 'DecisionWorkspace.tsx'), 'utf8')
  expect(workspace).not.toContain('已通过 TSP 审计')
  expect(workspace).not.toContain('胜率已验证')
})
