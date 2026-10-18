// 板块判断工具函数

export const BOARDS = ['沪主板', '深主板', '创业板', '科创板', '北交所'] as const
export type BoardType = (typeof BOARDS)[number]

// 号段口径与后端 price_limits.board_limit_pct 一致:
// 创业板 300/301/302 (302 自 2025-02-17 启用, 如 302132.SZ 中航成飞),
// 科创板 688/689 (689 为 CDR 号段, 如 689009.SH 九号公司)。
const CHINEXT_RE = /^(300|301|302)/
const STAR_RE = /^(688|689)/

/** 创业板 (300/301/302) */
export function isChiNext(symbol: string): boolean {
  return CHINEXT_RE.test(symbol)
}

/** 科创板 (688/689) */
export function isStar(symbol: string): boolean {
  return STAR_RE.test(symbol)
}

/** 根据股票代码判断板块 */
export function getBoardType(symbol: string): BoardType | null {
  if (isChiNext(symbol)) return '创业板'
  if (isStar(symbol)) return '科创板'
  if (/\.BJ$/.test(symbol)) return '北交所'
  if (/^60[0135]/.test(symbol)) return '沪主板'
  if (/^00[012]/.test(symbol)) return '深主板'
  return null
}

/** 板块简称标签: 主板返回空字符串(不显示), 创/科/北 等返回简称 */
export function boardTag(symbol: string): string {
  const b = getBoardType(symbol)
  if (!b) return ''
  if (b === '沪主板' || b === '深主板') return ''
  if (b === '创业板') return '创'
  if (b === '科创板') return '科'
  if (b === '北交所') return '北'
  return ''
}
