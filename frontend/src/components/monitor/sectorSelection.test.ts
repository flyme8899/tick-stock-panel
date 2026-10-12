import { describe, expect, it } from 'vitest'
import type { SectorMonitorTarget } from '@/lib/api'
import {
  SECTOR_TARGET_LIMIT,
  deselectVisibleSectorTargets,
  isSectorTargetUnavailable,
  sectorDirectionClass,
  selectVisibleSectorTargets,
} from './sectorSelection'

function target(partial: Partial<SectorMonitorTarget> & Pick<SectorMonitorTarget, 'key' | 'name'>): SectorMonitorTarget {
  return {
    kind: 'industry',
    available: true,
    member_count: 10,
    ...partial,
  }
}

describe('sector target selection', () => {
  it('selects only the visible available rows and stops at 20', () => {
    const hidden = target({ key: 'hidden', name: '已选但被搜索藏起' })
    const visible = [
      target({ key: 'off', name: '成分不足', member_count: 3 }),
      target({ key: 'gone', name: '无行情', available: false, member_count: 20 }),
      ...Array.from({ length: 25 }, (_, index) => target({ key: `v${index}`, name: `可见${index}` })),
    ]
    const next = selectVisibleSectorTargets([hidden], visible)
    expect(next[0]?.key).toBe('hidden')
    expect(next).toHaveLength(SECTOR_TARGET_LIMIT)
    expect(next.map(item => item.key)).not.toContain('off')
    expect(next.map(item => item.key)).not.toContain('gone')
    expect(next.map(item => item.key)).toContain('v18')
    expect(next.map(item => item.key)).not.toContain('v19')
  })

  it('deselects only rows currently visible after search', () => {
    const current = [
      target({ key: 'keep', name: '藏起来的' }),
      target({ key: 'drop', name: '看得见的' }),
    ]
    const visible = [target({ key: 'drop', name: '看得见的' }), target({ key: 'other', name: '未选' })]
    expect(deselectVisibleSectorTargets(current, visible).map(item => item.key)).toEqual(['keep'])
  })

  it('treats indices as selectable and small boards as unavailable', () => {
    expect(isSectorTargetUnavailable(target({ key: 'i', name: '上证', kind: 'index', member_count: 1 }))).toBe(false)
    expect(isSectorTargetUnavailable(target({ key: 's', name: '小行业', member_count: 4 }))).toBe(true)
  })

  it('paints up red and down green', () => {
    expect(sectorDirectionClass('up', true)).toContain('text-bull')
    expect(sectorDirectionClass('up', true)).not.toContain('text-bear')
    expect(sectorDirectionClass('down', true)).toContain('text-bear')
    expect(sectorDirectionClass('down', true)).not.toContain('text-bull')
    expect(sectorDirectionClass('up', false)).toContain('hover:text-bull')
    expect(sectorDirectionClass('down', false)).toContain('hover:text-bear')
  })
})
