// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import type { EtfRotationView } from './client'
import { EtfRotationPanel } from './DecisionWorkspace'

const view: EtfRotationView = {
  signal_date: '2026-09-30',
  mode: 'blended_bucket',
  mode_label: '分桶混合动量',
  holdings: [
    { code: '510300', name: '沪深300', bucket: 'A股', weight: 0.4, score: 1.5 },
    { code: '518880', name: '黄金', bucket: '518880', weight: 0.35, score: 2 },
    { code: '511880', name: '银华日利', bucket: '防守', weight: 0.25, score: null },
  ],
  last_rebalance: '2026-08-31',
  next_rebalance: '2026-10-09',
  disclaimer: '规则结果不代表未来收益，不构成投资建议。',
  score_kind: 'avg_rank',
  score_hint: '得分是 20、60、120 日收益的平均排名，越小越强。',
  position_basis: 'target',
}

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

it('shows the rotation card and keeps the raw log collapsed', async () => {
  await act(async () => {
    root.render(
      <EtfRotationPanel
        pending={false}
        error={null}
        detail={'INFO 拉到了日线\nTSP_ETF_RESULT should-not-be-here'}
        result={view}
        onRun={() => undefined}
      />,
    )
  })

  const text = host.textContent || ''
  expect(text).toContain('分桶混合动量')
  expect(text).toContain('信号日 2026-09-30')
  expect(text).toContain('沪深300 510300')
  expect(text).toContain('40.0%')
  expect(text).toContain('1.50')
  expect(text).toContain('上次调仓 2026-08-31')
  expect(text).toContain('下次调仓 2026-10-09')
  expect(text).toContain('不构成投资建议')
  expect(text).toContain('今日为调仓日')

  const logs = host.querySelector('details')
  expect(logs).not.toBeNull()
  expect(logs?.open).toBe(false)
  expect(logs?.querySelector('summary')?.textContent).toBe('运行日志')
  expect(logs?.textContent).toContain('INFO 拉到了日线')
})
