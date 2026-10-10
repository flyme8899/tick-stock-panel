// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { parseSectorTab, sectorAnalysisSearch, sectorPageLabel } from './sectorTab'

describe('parseSectorTab', () => {
  it('defaults unknown values to concept', () => {
    expect(parseSectorTab(null)).toBe('concept')
    expect(parseSectorTab(undefined)).toBe('concept')
    expect(parseSectorTab('')).toBe('concept')
    expect(parseSectorTab('foo')).toBe('concept')
    expect(parseSectorTab('concept')).toBe('concept')
  })

  it('accepts industry', () => {
    expect(parseSectorTab('industry')).toBe('industry')
  })
})

describe('sectorAnalysisSearch', () => {
  it('sets the tab and keeps other params', () => {
    expect(sectorAnalysisSearch('concept')).toBe('tab=concept')
    expect(sectorAnalysisSearch('industry', 'foo=1')).toBe('foo=1&tab=industry')
    expect(sectorAnalysisSearch('concept', new URLSearchParams('tab=industry&x=2'))).toBe('tab=concept&x=2')
  })
})

describe('sectorPageLabel', () => {
  it('names the merged page and the active tab', () => {
    expect(sectorPageLabel('/sector-analysis')).toBe('板块分析 · 概念')
    expect(sectorPageLabel('/sector-analysis', '?tab=industry')).toBe('板块分析 · 行业')
    expect(sectorPageLabel('/sector-analysis', '?tab=concept')).toBe('板块分析 · 概念')
    expect(sectorPageLabel('/concept-analysis')).toBe('板块分析 · 概念')
    expect(sectorPageLabel('/industry-analysis')).toBe('板块分析 · 行业')
    expect(sectorPageLabel('/fund-flow')).toBeNull()
  })
})
