import { describe, expect, it } from 'vitest'
import type { CandleMarker } from '@/lib/api'
import { isNearPriceExtreme, selectPatternMarkers, type PatternPriceBar } from './patternMarkers'

function day(index: number): string {
  const date = new Date(Date.UTC(2026, 7, 10 + index))
  return date.toISOString().slice(0, 10)
}

/** 缓慢抬升的 K 线。最后一根拔高，便于把十字放在极值上。 */
function risingBars(count: number, peakAt?: number): PatternPriceBar[] {
  return Array.from({ length: count }, (_, index) => {
    const base = 30 + index * 0.2
    const high = index === peakAt ? base + 8 : base + 0.6
    return { date: day(index), high, low: base }
  })
}

function marker(index: number, names: string[], side: CandleMarker['side'] = 'bull'): CandleMarker {
  return { date: day(index), side, name: names.join('·'), patterns: names }
}

describe('selectPatternMarkers', () => {
  it('丢掉长蜡烛、纺锤这类低价值形态', () => {
    const rows = risingBars(12)
    const marks = selectPatternMarkers([
      marker(1, ['长蜡烛', '短蜡烛', '纺锤']),
      marker(2, ['孕线', '陷阱', '风高浪大']),
    ], rows)
    expect(marks).toEqual([])
  })

  it('同一根只留更重要的短标签', () => {
    const rows = risingBars(12)
    const marks = selectPatternMarkers([
      marker(4, ['长蜡烛', '纺锤', '吞没', '锤头'], 'bull'),
      marker(4, ['暮星'], 'bear'),
    ], rows)
    expect(marks).toEqual([
      { date: day(4), kind: 'sell', label: '暮星', patternTag: true, above: true },
    ])
  })

  it('横盘没有可辨极值时不标十字', () => {
    const rows = Array.from({ length: 15 }, (_, index) => ({
      date: day(index), high: 10.01, low: 10,
    }))
    expect(isNearPriceExtreme(rows, 7)).toBe(false)
    expect(selectPatternMarkers([marker(7, ['十字'], 'neutral')], rows)).toEqual([])
  })

  it('看跌形态放在蜡烛上方，标签用短名', () => {
    const rows = risingBars(12)
    const marks = selectPatternMarkers([
      marker(3, ['上吊线'], 'bear'),
      marker(9, ['黄昏之星'], 'bear'),
    ], rows)
    expect(marks.map(item => [item.label, item.kind, item.above])).toEqual([
      ['上吊', 'sell', true],
      ['暮星', 'sell', true],
    ])
  })

  it('相邻日期重叠时留下更重要的，同等重要留下更晚的', () => {
    const rows = risingBars(20)
    const crowded = selectPatternMarkers([
      marker(2, ['吞没'], 'bull'),
      marker(4, ['晨星'], 'bull'),
      marker(6, ['锤子'], 'bull'),
    ], rows)
    expect(crowded.map(item => item.label)).toEqual(['晨星'])

    const tied = selectPatternMarkers([
      marker(1, ['吞没'], 'bull'),
      marker(3, ['吞没'], 'bear'),
    ], rows)
    expect(tied.map(item => [item.date, item.label, item.kind])).toEqual([
      [day(3), '吞没', 'sell'],
    ])
  })

  it('间隔够开的重要形态都保留', () => {
    const rows = risingBars(24)
    const marks = selectPatternMarkers([
      marker(1, ['三个白兵'], 'bull'),
      marker(8, ['三只乌鸦'], 'bear'),
      marker(16, ['吞没'], 'bull'),
    ], rows)
    expect(marks.map(item => item.label)).toEqual(['三兵', '三鸦', '吞没'])
  })

  it('十字只标在局部极值附近', () => {
    const rows = risingBars(30, 29)
    expect(isNearPriceExtreme(rows, 29)).toBe(true)
    expect(isNearPriceExtreme(rows, 14)).toBe(false)

    const marks = selectPatternMarkers([
      marker(14, ['十字星', '黄包车夫'], 'neutral'),
      marker(29, ['墓碑十字'], 'bear'),
    ], rows)
    expect(marks.map(item => [item.date, item.label, item.above])).toEqual([
      [day(29), '十字', true],
    ])
  })

  it('没有 patterns 字段时从拼接名字里拆', () => {
    const rows = risingBars(8)
    const marks = selectPatternMarkers([
      { date: day(2), side: 'bull', name: '长蜡烛·早晨之星·纺锤+2', patterns: [] },
    ], rows)
    expect(marks.map(item => item.label)).toEqual(['晨星'])
  })

  it('空数据、对不上的日期都不画', () => {
    const rows = risingBars(4)
    expect(selectPatternMarkers(undefined, rows)).toEqual([])
    expect(selectPatternMarkers([], rows)).toEqual([])
    expect(selectPatternMarkers([marker(1, ['吞没'])], [])).toEqual([])
    expect(selectPatternMarkers([
      { date: '1999-01-01', side: 'bull', name: '吞没', patterns: ['吞没'] },
    ], rows)).toEqual([])
  })
})
