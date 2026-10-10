/** 侧栏菜单顺序里，概念分析 / 行业分析合并为板块分析后的 id 迁移。 */

export const SECTOR_ANALYSIS_PATH = '/sector-analysis'

export const LEGACY_SECTOR_ANALYSIS_PATHS = ['/concept-analysis', '/industry-analysis'] as const

const LEGACY_SECTOR_IDS = new Set<string>(LEGACY_SECTOR_ANALYSIS_PATHS)

function isSectorNavId(id: string): boolean {
  return id === SECTOR_ANALYSIS_PATH || LEGACY_SECTOR_IDS.has(id)
}

/**
 * 把已保存的菜单顺序里的旧页面 id 收成一个「板块分析」。
 * 两个旧 id 都在时保留先出现的位置，丢掉后一个，避免合并后多出一条或跳回默认位。
 */
export function migrateNavOrder(ids: string[]): string[] {
  let placed = false
  const out: string[] = []
  for (const id of ids) {
    if (!isSectorNavId(id)) {
      out.push(id)
      continue
    }
    if (placed) continue
    out.push(SECTOR_ANALYSIS_PATH)
    placed = true
  }
  return out
}

/**
 * 隐藏列表同步收成新 id。
 * 只隐藏了概念或只隐藏了行业时，合并页仍显示——另一页还在用。
 * 两个旧页都隐藏，或已经隐藏了新 id，合并页才隐藏。
 */
export function migrateNavHidden(hidden: string[]): string[] {
  const hiddenSet = new Set(hidden)
  const bothLegacyHidden = LEGACY_SECTOR_ANALYSIS_PATHS.every(id => hiddenSet.has(id))
  const hideSector = hiddenSet.has(SECTOR_ANALYSIS_PATH) || bothLegacyHidden
  let placed = false
  const out: string[] = []
  for (const id of hidden) {
    if (!isSectorNavId(id)) {
      out.push(id)
      continue
    }
    if (!hideSector || placed) continue
    out.push(SECTOR_ANALYSIS_PATH)
    placed = true
  }
  return out
}
