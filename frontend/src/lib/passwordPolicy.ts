/** 自助改密规则，与后端 validate_replacement_password 一致。首次设密仍是至少 6 位。 */

const REPLACEMENT_MIN = 8
const PASSWORD_MAX = 128

export function replacementPasswordError(next: string, current: string): string | null {
  if (next === current) return '新密码不能与当前密码相同'
  if (next.length < REPLACEMENT_MIN) return `新密码至少 ${REPLACEMENT_MIN} 位`
  if (next.length > PASSWORD_MAX) return `密码不能超过 ${PASSWORD_MAX} 位`
  let letters = false
  let digits = false
  let symbols = false
  for (const ch of next) {
    if (/\p{L}/u.test(ch)) letters = true
    else if (/\p{Nd}/u.test(ch)) digits = true
    else if (!/\s/u.test(ch)) symbols = true
  }
  if (Number(letters) + Number(digits) + Number(symbols) < 2) {
    return '新密码须混合字母、数字、符号中的至少两类'
  }
  return null
}
