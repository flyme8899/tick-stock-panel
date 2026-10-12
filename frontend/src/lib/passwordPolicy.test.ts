import { describe, expect, it } from 'vitest'
import { replacementPasswordError } from './passwordPolicy'

describe('replacementPasswordError', () => {
  it('rejects a short, single-class, or unchanged password', () => {
    expect(replacementPasswordError('short1', 'old-password-1')).toBe('新密码至少 8 位')
    expect(replacementPasswordError('abcdefgh', 'old-password-1')).toMatch(/至少两类/)
    expect(replacementPasswordError('12345678', 'old-password-1')).toMatch(/至少两类/)
    expect(replacementPasswordError('!!!!!!!!', 'old-password-1')).toMatch(/至少两类/)
    expect(replacementPasswordError('abcd    ', 'old-password-1')).toMatch(/至少两类/)
    expect(replacementPasswordError('old-password-1', 'old-password-1')).toBe('新密码不能与当前密码相同')
  })

  it('accepts two character classes', () => {
    expect(replacementPasswordError('abc12345', 'old-password-1')).toBeNull()
    expect(replacementPasswordError('abcdefg!', 'old-password-1')).toBeNull()
    expect(replacementPasswordError('1234567!', 'old-password-1')).toBeNull()
  })
})
