import type { SectorMonitorTarget } from '@/lib/api'

/** 普通多选上限。全A 不走这条路径。 */
export const SECTOR_TARGET_LIMIT = 20

export function isSectorTargetUnavailable(target: Pick<SectorMonitorTarget, 'available' | 'kind' | 'member_count'>): boolean {
  return !target.available || (target.kind !== 'index' && target.member_count < 5)
}

/** 把当前可见、可选的对象并入已选, 直到达到上限。已选项和被搜索藏起来的项保持不变。 */
export function selectVisibleSectorTargets(
  current: SectorMonitorTarget[],
  visible: SectorMonitorTarget[],
  limit = SECTOR_TARGET_LIMIT,
): SectorMonitorTarget[] {
  const selected = new Set(current.map(target => target.key))
  const next = [...current]
  for (const target of visible) {
    if (next.length >= limit) break
    if (isSectorTargetUnavailable(target) || selected.has(target.key)) continue
    next.push(target)
    selected.add(target.key)
  }
  return next
}

/** 只取消当前可见项。搜索过滤掉的已选项保留。 */
export function deselectVisibleSectorTargets(
  current: SectorMonitorTarget[],
  visible: SectorMonitorTarget[],
): SectorMonitorTarget[] {
  const visibleKeys = new Set(visible.map(target => target.key))
  return current.filter(target => !visibleKeys.has(target.key))
}

/** 上涨红、下跌绿。未选中时保留同色的悬停提示。 */
export function sectorDirectionClass(direction: 'up' | 'down', active: boolean): string {
  if (direction === 'up') {
    return active ? 'bg-bull/15 text-bull' : 'text-muted hover:text-bull'
  }
  return active ? 'bg-bear/15 text-bear' : 'text-muted hover:text-bear'
}
