// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { QuantEvidencePanel } from './DecisionWorkspace'
import type { DsaQuantEvidence } from './client'

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

const data: DsaQuantEvidence = {
  available: true,
  meta: {
    source: 'tsp',
    report: 'sample',
    period: '2024-01 至 2026-09',
    universe: 5200,
    benchmark_return: '+18.40%',
    updated: '2026-10-01',
  },
  caveat: '机械回测，不是本策略的实盘业绩。',
  skills: [
    { name: 'bull_trend', matched: ['趋势'], returns: [12.5, -3.2], sample_sizes: [40], verdict: '', note: '', guidance: '' },
    { name: 'mean_revert', matched: [], returns: [-8.15], sample_sizes: [12], verdict: '', note: '', guidance: '' },
  ],
}

it('keeps quant evidence collapsed until opened and colors gains red, losses green', async () => {
  await act(async () => {
    root.render(<QuantEvidencePanel data={data} />)
  })
  const toggle = [...host.querySelectorAll('button')].find(button => button.textContent?.includes('量化回测参考'))
  expect(toggle?.getAttribute('aria-expanded')).toBe('false')
  expect(host.textContent).not.toContain('+12.50%')
  expect(host.textContent).not.toContain('机械回测')

  await act(async () => {
    toggle?.click()
  })
  expect(toggle?.getAttribute('aria-expanded')).toBe('true')
  expect(host.textContent).toContain('+12.50%')
  expect(host.textContent).toContain('-3.20%')
  expect(host.textContent).toContain('-8.15%')
  expect(host.querySelector('.text-bull')?.textContent).toBe('+12.50%')
  expect(host.querySelector('.text-bear')?.textContent).toBe('-3.20%')
})
