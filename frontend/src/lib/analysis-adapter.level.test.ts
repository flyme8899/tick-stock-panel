// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { groupByIndustryLevel, industryLevelName, type DimensionGroup } from './analysis-adapter'

function group(key: string, symbols: string[]): DimensionGroup {
  return {
    key,
    count: symbols.length,
    stocks: symbols.map(symbol => ({ symbol })),
    metrics: {},
  }
}

describe('industryLevelName', () => {
  it('reads the requested segment and falls back to the last one', () => {
    expect(industryLevelName('银行-国有大行-大型', 1)).toBe('银行')
    expect(industryLevelName('银行-国有大行-大型', 2)).toBe('国有大行')
    expect(industryLevelName('银行-国有大行-大型', 3)).toBe('大型')
    expect(industryLevelName('银行', 3)).toBe('银行')
    expect(industryLevelName('  房地产 - 住宅开发 ', 2)).toBe('住宅开发')
  })
})

describe('groupByIndustryLevel', () => {
  const groups = [
    group('银行-国有大行-大型', ['000001.SZ']),
    group('银行-股份制-中型', ['600000.SH']),
    group('房地产-住宅开发-全国', ['000002.SZ']),
  ]

  it('merges constituents into the chosen level and sorts by count', () => {
    const level1 = groupByIndustryLevel(groups, 1)
    expect(level1.map(g => [g.key, g.count, g.stocks.map(s => s.symbol)])).toEqual([
      ['银行', 2, ['000001.SZ', '600000.SH']],
      ['房地产', 1, ['000002.SZ']],
    ])

    const level2 = groupByIndustryLevel(groups, 2)
    expect(level2.map(g => g.key).sort()).toEqual(['住宅开发', '国有大行', '股份制'])
    expect(level2.every(g => g.count === 1)).toBe(true)
  })

  it('does not mutate the source groups', () => {
    const source = [group('银行-国有大行-大型', ['000001.SZ'])]
    groupByIndustryLevel(source, 1)
    expect(source[0].stocks).toEqual([{ symbol: '000001.SZ' }])
    expect(source[0].key).toBe('银行-国有大行-大型')
  })
})
