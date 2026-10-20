import { describe, expect, it } from 'vitest'
import {
  REFERENCE_NOTE,
  REVERSE_NOTE,
  normalizeStatus,
  showsBuyPoint,
  validationLabel,
  validationNote,
} from './strategyValidation'

describe('策略验证状态展示', () => {
  it('缺字段时默认仅供参考', () => {
    expect(normalizeStatus(undefined)).toBe('reference_only')
    expect(normalizeStatus('nope')).toBe('reference_only')
    expect(validationNote(undefined, '')).toBe(REFERENCE_NOTE)
    expect(showsBuyPoint(undefined)).toBe(true)
  })

  it('反向不作为买点，并带体检提示语', () => {
    expect(showsBuyPoint('reverse')).toBe(false)
    expect(showsBuyPoint('retired')).toBe(false)
    expect(validationLabel('reverse')).toBe('风险提示')
    expect(validationNote('reverse', REVERSE_NOTE)).toBe(REVERSE_NOTE)
    expect(validationLabel('retired')).toBe('已下线')
    expect(validationNote('unverified', '导出失败')).toBe('导出失败')
  })
})
