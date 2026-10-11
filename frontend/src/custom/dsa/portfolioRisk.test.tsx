// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { PortfolioRiskView } from './portfolioRisk'
import { SAMPLE_RISK } from './samples'
import { BacktestSummary, CapabilitySummary, ImportSummary, UsageSummary } from './DecisionWorkspace'

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

async function render(node: ReactNode) {
  await act(async () => {
    root.render(<>{node}</>)
  })
}

const emptyRisk = {
  as_of: '2026-10-10',
  currency: 'CNY',
  thresholds: { concentration_alert_pct: 35, drawdown_alert_pct: 15, stop_loss_alert_pct: 10, stop_loss_near_ratio: 0.8, lookback_days: 180 },
  concentration: { total_market_value: 0, top_weight_pct: 0, alert: false, top_positions: [] },
  sector_concentration: { top_weight_pct: 0, alert: false, top_sectors: [] },
  drawdown: { series_points: 0, max_drawdown_pct: 0, current_drawdown_pct: 0, alert: false, fx_stale: false },
  stop_loss: { near_alert: false, triggered_count: 0, near_count: 0, items: [] },
}

it('shows an empty state when the DSA book has no positions', async () => {
  await render(<PortfolioRiskView data={emptyRisk} />)
  expect(host.textContent).toContain('DSA 账本暂无持仓')
  expect(host.textContent).toContain('录入持仓后会显示风险。')
  expect(host.querySelector('table')).toBeNull()
})

it('renders thresholds, concentration, sectors, drawdown, and stop-loss', async () => {
  await render(<PortfolioRiskView data={SAMPLE_RISK} />)
  expect(host.querySelector('[data-risk-thresholds]')?.textContent).toContain('35.00%')
  expect(host.querySelector('[data-risk-thresholds]')?.textContent).toContain('80%')
  expect(host.querySelector('[data-risk-concentration]')?.textContent).toContain('1,280,000.00')
  expect(host.querySelector('[data-risk-concentration]')?.textContent).toContain('42.50%')
  expect(host.textContent).toContain('超阈值')
  const table = host.querySelector('table')
  expect(table?.parentElement?.className).toContain('overflow-x-auto')
  expect(table?.textContent).toContain('600519')
  expect(host.querySelector('[data-risk-sectors]')?.textContent).toContain('67.50%')
  expect(host.textContent).toContain('白酒')
  expect(host.querySelector('[data-risk-drawdown]')?.textContent).toContain('3.10%')
  expect(host.querySelector('[data-risk-drawdown]')?.textContent).toContain('8.20%')
  expect(host.querySelector('[data-risk-stop]')?.textContent).toContain('接近 1 只')
  expect(host.textContent).toContain('12.50%')
  expect(host.textContent).not.toContain('engine_version')
})

it('renders the other upstream payloads as summaries', async () => {
  await render(
    <>
      <CapabilitySummary data={{ providers: [{ name: 'tickflow', label: 'TickFlow', status: 'ok', markets: ['cn'], datasets: ['daily'] }], datasets: [], priorities: [] }} />
      <BacktestSummary data={{ processed: 4, saved: 3, completed: 2, insufficient: 1, errors: 0, applied_eval_window_days: 5, message: '有一条样本不足' }} />
      <ImportSummary data={{ codes: ['600519'], items: [{ code: '600519', name: '贵州茅台', confidence: 'high' }] }} />
      <UsageSummary data={{ period: 'today', from_date: '2026-10-11', to_date: '2026-10-11', total_calls: 2, total_prompt_tokens: 100, total_completion_tokens: 40, total_tokens: 140, by_call_type: [{ call_type: 'analysis', calls: 2, total_tokens: 140 }], by_model: [{ model: 'demo', calls: 2 }] }} />
    </>,
  )
  expect(host.textContent).toContain('TickFlow')
  expect(host.textContent).toContain('可用')
  expect(host.querySelector('[data-backtest-summary]')?.textContent).toContain('候选')
  expect(host.textContent).toContain('有一条样本不足')
  expect(host.querySelector('table')?.textContent).toContain('贵州茅台')
  expect(host.querySelector('table')?.parentElement?.className).toContain('overflow-x-auto')
  expect(host.querySelector('[data-usage-summary]')?.textContent).toContain('2')
  expect(host.textContent).toContain('个股分析')
  expect(host.querySelector('pre')).toBeNull()
})
