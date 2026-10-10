/** 板块分析页的概念 / 行业标签。缺省和无法识别的值都落在概念。 */

export type SectorKind = 'concept' | 'industry'

export function parseSectorTab(value: string | null | undefined): SectorKind {
  return value === 'industry' ? 'industry' : 'concept'
}

/** 保留当前查询参数，只改 tab。供页内切换和旧路由重定向共用。 */
export function sectorAnalysisSearch(tab: SectorKind, current?: URLSearchParams | string): string {
  const next = new URLSearchParams(current ?? '')
  next.set('tab', tab)
  return next.toString()
}

/** 助手页面上下文：合并页带上当前标签，旧路径也报到对应标签。 */
export function sectorPageLabel(pathname: string, search = ''): string | null {
  if (pathname === '/industry-analysis') return '板块分析 · 行业'
  if (pathname === '/concept-analysis') return '板块分析 · 概念'
  if (pathname === '/sector-analysis') {
    return parseSectorTab(new URLSearchParams(search).get('tab')) === 'industry'
      ? '板块分析 · 行业'
      : '板块分析 · 概念'
  }
  return null
}
