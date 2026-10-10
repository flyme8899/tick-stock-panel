/** 板块分析页的两列：概念与行业同时展示，旧入口用 focus 定位其中一列。 */

export type SectorKind = 'concept' | 'industry'

/** 只接受明确的 concept / industry。空值和旧的无法识别参数都不聚焦某一列。 */
export function parseSectorFocus(value: string | null | undefined): SectorKind | null {
  if (value === 'concept' || value === 'industry') return value
  return null
}

/** 保留当前查询参数，改成聚焦某一列。顺手清掉上一版的 tab 参数。 */
export function sectorFocusSearch(focus: SectorKind, current?: URLSearchParams | string): string {
  const next = new URLSearchParams(current ?? '')
  next.delete('tab')
  next.set('focus', focus)
  return next.toString()
}

/**
 * 助手页面上下文。
 * 并排页默认报「板块分析」；旧路径和 focus 参数说明用户正看着哪一列。
 */
export function sectorPageLabel(pathname: string, search = ''): string | null {
  if (pathname === '/industry-analysis') return '板块分析 · 行业'
  if (pathname === '/concept-analysis') return '板块分析 · 概念'
  if (pathname === '/sector-analysis') {
    const params = new URLSearchParams(search)
    const focus = parseSectorFocus(params.get('focus') ?? params.get('tab'))
    if (focus === 'industry') return '板块分析 · 行业'
    if (focus === 'concept') return '板块分析 · 概念'
    return '板块分析'
  }
  return null
}
