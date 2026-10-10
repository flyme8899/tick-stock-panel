// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { groupByIndustryLevel, industryLevelName, isMembershipTag, resolveDimension, selectSectorConfig, type DimensionGroup } from './analysis-adapter'

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

  it('does not treat a concept-labeled table as an industry source', () => {
    const configs = [
      {
        id: 'ext_gn_ths',
        fields: [{ name: '所属概念', label: '所属概念' }],
      },
      {
        id: 'board_mix',
        fields: [{ name: '所属概念', label: '概念 sector' }],
      },
      {
        id: 'ext_hy_ths',
        fields: [{ name: '所属同花顺行业', label: '所属同花顺行业' }],
      },
      {
        id: 'sw_industry',
        fields: [{ name: '申万行业', label: '申万一级行业' }],
      },
    ]
    expect(selectSectorConfig('industry', configs)?.id).toBe('ext_hy_ths')
    expect(selectSectorConfig('industry', configs, 'ext_gn_ths')?.id).toBe('ext_hy_ths')
    expect(selectSectorConfig('industry', configs, 'board_mix')?.id).toBe('ext_hy_ths')
    expect(selectSectorConfig('industry', configs, 'sw_industry')?.id).toBe('sw_industry')
    expect(selectSectorConfig('concept', configs)?.id).toBe('ext_gn_ths')
    expect(selectSectorConfig('industry', configs.filter(item => item.id !== 'ext_hy_ths'))?.id).toBe('sw_industry')
  })

  it('does not group concept rows when the industry column asks for an industry field', () => {
    const fields = [{ name: 'symbol', dtype: 'str', label: '代码' }, { name: '所属概念', dtype: 'str', label: '概念 sector' }]
    const data = {
      id: 'board_mix',
      label: '行业板块',
      mode: 'snapshot' as const,
      date: null,
      total: 1,
      limit: 10,
      fields,
      rows: [{ symbol: '000001.SZ', 所属概念: '融资融券' }],
    }
    const config = { id: 'board_mix', label: '行业板块', mode: 'snapshot' as const, fields, created_at: '', updated_at: '' }
    const resolved = resolveDimension(data, config, ['行业', 'sector'], { kind: 'industry' })
    expect(resolved.ok).toBe(false)
    expect(resolved.groups).toEqual([])
  })
})

describe('isMembershipTag', () => {
  it('matches index and margin tags without swallowing real industries', () => {
    expect(isMembershipTag('融资融券')).toBe(true)
    expect(isMembershipTag('深股通')).toBe(true)
    expect(isMembershipTag('沪深300')).toBe(true)
    expect(isMembershipTag('MSCI中国')).toBe(true)
    expect(isMembershipTag('标普道琼斯A股')).toBe(true)
    expect(isMembershipTag('转融券标的')).toBe(true)
    expect(isMembershipTag('HS300')).toBe(true)
    expect(isMembershipTag('机器人')).toBe(false)
    expect(isMembershipTag('银行')).toBe(false)
    expect(isMembershipTag('机械设备')).toBe(false)
  })
})

describe('groupByIndustryLevel source stability', () => {
  it('does not mutate the source groups', () => {
    const source = [group('银行-国有大行-大型', ['000001.SZ'])]
    groupByIndustryLevel(source, 1)
    expect(source[0].stocks).toEqual([{ symbol: '000001.SZ' }])
    expect(source[0].key).toBe('银行-国有大行-大型')
  })
})
