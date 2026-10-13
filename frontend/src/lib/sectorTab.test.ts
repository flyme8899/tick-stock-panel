import { describe, expect, it } from 'vitest'
import {
  parseSectorFocus,
  parseSectorKey,
  sectorFocusSearch,
  sectorPageLabel,
  sectorTargetSearch,
} from './sectorTab'

describe('parseSectorFocus', () => {
  it('ignores empty and unknown values', () => {
    expect(parseSectorFocus(null)).toBeNull()
    expect(parseSectorFocus(undefined)).toBeNull()
    expect(parseSectorFocus('')).toBeNull()
    expect(parseSectorFocus('foo')).toBeNull()
  })

  it('accepts the two columns', () => {
    expect(parseSectorFocus('concept')).toBe('concept')
    expect(parseSectorFocus('industry')).toBe('industry')
  })
})

describe('sectorFocusSearch', () => {
  it('sets focus and drops the old tab param', () => {
    expect(sectorFocusSearch('concept')).toBe('focus=concept')
    expect(sectorFocusSearch('industry', 'foo=1')).toBe('foo=1&focus=industry')
    expect(sectorFocusSearch('concept', new URLSearchParams('tab=industry&x=2'))).toBe('x=2&focus=concept')
  })

  it('drops a stale sector param so switching focus does not keep a foreign board selected', () => {
    // 从「概念-某题材」切到行业列时, 旧的 sector 属于概念列, 留着会让行业列找不到板块。
    expect(sectorFocusSearch('industry', 'focus=concept&sector=银行概念')).toBe('focus=industry')
  })
})

describe('parseSectorKey', () => {
  it('treats blank values as no selection', () => {
    expect(parseSectorKey(null)).toBeNull()
    expect(parseSectorKey(undefined)).toBeNull()
    expect(parseSectorKey('')).toBeNull()
    expect(parseSectorKey('   ')).toBeNull()
  })

  it('keeps the board name, trimmed', () => {
    expect(parseSectorKey(' 银行 ')).toBe('银行')
    expect(parseSectorKey('银行-国有大行-大型')).toBe('银行-国有大行-大型')
  })
})

describe('sectorTargetSearch', () => {
  it('carries both the focused column and the board', () => {
    expect(sectorTargetSearch('concept', '机器人')).toBe('focus=concept&sector=%E6%9C%BA%E5%99%A8%E4%BA%BA')
    expect(sectorTargetSearch('industry', '银行', 'foo=1')).toBe('foo=1&focus=industry&sector=%E9%93%B6%E8%A1%8C')
  })

  it('replaces an older target instead of appending a second one', () => {
    const once = sectorTargetSearch('concept', '机器人')
    const twice = sectorTargetSearch('concept', '低空经济', once)
    expect(new URLSearchParams(twice).getAll('sector')).toEqual(['低空经济'])
    expect(new URLSearchParams(twice).get('focus')).toBe('concept')
  })
})

describe('sectorPageLabel', () => {
  it('names the page, and the focused column when one is requested', () => {
    expect(sectorPageLabel('/sector-analysis')).toBe('板块分析')
    expect(sectorPageLabel('/sector-analysis', '?focus=industry')).toBe('板块分析 · 行业')
    expect(sectorPageLabel('/sector-analysis', '?focus=concept')).toBe('板块分析 · 概念')
    expect(sectorPageLabel('/sector-analysis', '?tab=industry')).toBe('板块分析 · 行业')
    expect(sectorPageLabel('/concept-analysis')).toBe('板块分析 · 概念')
    expect(sectorPageLabel('/industry-analysis')).toBe('板块分析 · 行业')
    expect(sectorPageLabel('/fund-flow')).toBeNull()
  })
})
