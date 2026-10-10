import { describe, expect, it } from 'vitest'
import { parseSectorFocus, sectorFocusSearch, sectorPageLabel } from './sectorTab'

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
