import { expect, it } from 'vitest'
import { applyFilter, defaultFilter } from './ScreenerFilter'

it('筹码筛选按百分数比较，空值通不过已填写的条件', () => {
  const rows = [
    {
      symbol: '300829.SZ', close: 10, name: '示例',
      benefit_ratio: 0.2, avg_cost_bias: 0.05, concentration_90: 0.1, concentration_70: 0.08,
    },
    { symbol: '002212.SZ', close: 10, name: '缺筹码', benefit_ratio: null },
  ]
  const tight = applyFilter(rows, { ...defaultFilter, benefitMin: '30' })
  expect(tight).toEqual([])
  const wide = applyFilter(rows, { ...defaultFilter, benefitMax: '25' })
  expect(wide.map(row => row.symbol)).toEqual(['300829.SZ'])
})
