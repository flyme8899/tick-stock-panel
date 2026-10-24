/**
 * 表格排序 hook（三态：无 → 升序 → 降序 → 无）。
 *
 * 从自选页提炼，自选/策略列表共享。调用方提供 getSortValue 决定每列的排序标量。
 */
import { useCallback, useState } from 'react'
import type { ColumnConfig } from '@/lib/list-columns'
import { getSortValue as defaultGetSortValue } from '@/lib/stock-table'

export interface SortState {
  key: string   // 列 id
  dir: 'asc' | 'desc'
}

export function useTableSort<T>(getSortValue: (r: T, col: ColumnConfig) => any = defaultGetSortValue) {
  const [sort, setSort] = useState<SortState | null>(null)

  /** 点击表头：同列轮换 asc→desc→清除；不同列重置为 asc */
  const toggle = useCallback((colId: string) => {
    setSort(prev => {
      if (!prev || prev.key !== colId) return { key: colId, dir: 'asc' }
      if (prev.dir === 'asc') return { key: colId, dir: 'desc' }
      return null
    })
  }, [])

  /** 对行集合按当前 sort 排序（返回新数组）。无 sort 或列不存在时原样返回；
   *  取值为 null 的行排在最后。表头是否可点由 StockDataTable 控制。 */
  const sortRows = useCallback((rows: T[], columns: ColumnConfig[]): T[] => {
    if (!sort) return rows
    const col = columns.find(c => c.id === sort.key)
    if (!col) return rows
    const { dir } = sort
    return [...rows].sort((a, b) => {
      const va = getSortValue(a, col)
      const vb = getSortValue(b, col)
      if (va == null && vb == null) return 0
      if (va == null) return 1
      if (vb == null) return -1
      const na = typeof va === 'number' ? va : Number(va)
      const nb = typeof vb === 'number' ? vb : Number(vb)
      // 用 isFinite 而非 isNaN: 排序键里出现 Infinity 时, `nb - na` 是 NaN 或
      // Infinity, 比较器不再满足全序, 数组顺序会变成引擎实现相关的乱序。归到
      // 下面的字符串比较分支, 至少结果稳定可复现。
      if (Number.isFinite(na) && Number.isFinite(nb)) {
        return dir === 'asc' ? na - nb : nb - na
      }
      const sa = String(va), sb = String(vb)
      return dir === 'asc' ? sa.localeCompare(sb) : sb.localeCompare(sa)
    })
  }, [sort, getSortValue])

  return { sort, toggle, sortRows }
}
