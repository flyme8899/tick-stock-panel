/**
 * 日 K「形态」标签的展示过滤。
 *
 * 只决定图表上画哪些短标签。选股、自定义信号和 candle_markers 接口仍使用完整形态目录，
 * 不读这里的白名单。
 */
import type { CandleMarker } from '@/lib/api'
import type { ChartMarker } from '@/components/EChartsCandlestick'

interface PatternDisplay {
  /** 图上短标签，两字为主，避免挡住蜡烛 */
  label: string
  /** 越大越优先；同一根或相邻重叠时留下这一档 */
  rank: number
  /** 十字类只有贴着局部高低点才标 */
  extremeOnly?: boolean
}

/** 高置信度形态。名字与蜡烛目录的中文名对齐，并兼容口头说法。 */
const PATTERN_DISPLAY: Record<string, PatternDisplay> = {
  三个白兵: { label: '三兵', rank: 80 },
  三只乌鸦: { label: '三鸦', rank: 80 },
  三胞胎乌鸦: { label: '三鸦', rank: 76 },
  晨星: { label: '晨星', rank: 70 },
  早晨之星: { label: '晨星', rank: 70 },
  暮星: { label: '暮星', rank: 70 },
  黄昏之星: { label: '暮星', rank: 70 },
  十字晨星: { label: '晨星', rank: 68 },
  十字暮星: { label: '暮星', rank: 68 },
  吞没: { label: '吞没', rank: 60 },
  锤头: { label: '锤子', rank: 50 },
  锤子: { label: '锤子', rank: 50 },
  上吊线: { label: '上吊', rank: 50 },
  上吊: { label: '上吊', rank: 50 },
  蜻蜓十字: { label: '十字', rank: 30, extremeOnly: true },
  墓碑十字: { label: '十字', rank: 30, extremeOnly: true },
  十字星: { label: '十字', rank: 24, extremeOnly: true },
  十字: { label: '十字', rank: 22, extremeOnly: true },
  长脚十字: { label: '十字', rank: 20, extremeOnly: true },
}

/**
 * 相邻标签的最小交易日间隔。
 * 两字标签在窄屏、约 60 根可见 K 线时大约横跨 5 根，更近就会叠在一起。
 */
const LABEL_GAP = 5

/** 局部高低点窗口（左右各 N 根）。十字只在这个窗口的上下沿附近出现。 */
const EXTREME_RADIUS = 10

/** 距离窗口最高/最低不超过区间的这个比例，算「极值附近」。 */
const EXTREME_BAND = 0.12

export interface PatternPriceBar {
  date: string
  high: number
  low: number
}

function patternNames(marker: CandleMarker): string[] {
  if (marker.patterns?.length) return marker.patterns
  return marker.name.replace(/\+\d+$/, '').split('·').map(part => part.trim()).filter(Boolean)
}

function bestDisplay(names: string[]): PatternDisplay | null {
  let best: PatternDisplay | null = null
  for (const name of names) {
    const spec = PATTERN_DISPLAY[name]
    if (!spec) continue
    if (!best || spec.rank > best.rank) best = spec
  }
  return best
}

function windowBounds(bars: PatternPriceBar[], index: number) {
  const start = Math.max(0, index - EXTREME_RADIUS)
  const end = Math.min(bars.length - 1, index + EXTREME_RADIUS)
  let maxHigh = -Infinity
  let minLow = Infinity
  for (let i = start; i <= end; i++) {
    maxHigh = Math.max(maxHigh, bars[i].high)
    minLow = Math.min(minLow, bars[i].low)
  }
  return { maxHigh, minLow, span: maxHigh - minLow }
}

/** 这根 K 线是否贴着左右窗口里的高点或低点。区间太平则不算极值。 */
export function isNearPriceExtreme(bars: PatternPriceBar[], index: number): boolean {
  const bar = bars[index]
  if (!bar) return false
  const { maxHigh, minLow, span } = windowBounds(bars, index)
  const mid = (maxHigh + minLow) / 2
  if (!(span > 0) || (mid > 0 && span / mid < 0.01)) return false
  return (maxHigh - bar.high) <= span * EXTREME_BAND || (bar.low - minLow) <= span * EXTREME_BAND
}

function closerToHigh(bars: PatternPriceBar[], index: number): boolean {
  const bar = bars[index]
  const { maxHigh, minLow } = windowBounds(bars, index)
  return (maxHigh - bar.high) <= (bar.low - minLow)
}

interface Candidate {
  date: string
  index: number
  rank: number
  label: string
  side: CandleMarker['side']
  above: boolean
}

/**
 * 从接口返回的形态标记里挑出图上要画的短标签。
 * 同一根只留更重要的一个；间隔不足 LABEL_GAP 时也只留更重要的，同等重要留更晚的一根。
 */
export function selectPatternMarkers(
  markers: CandleMarker[] | undefined,
  rows: PatternPriceBar[],
): ChartMarker[] {
  if (!markers?.length || rows.length === 0) return []
  const indexByDate = new Map<string, number>()
  rows.forEach((row, index) => indexByDate.set(row.date.slice(0, 10), index))

  const byDate = new Map<string, Candidate>()
  for (const marker of markers) {
    const date = marker.date.slice(0, 10)
    const index = indexByDate.get(date)
    if (index == null) continue
    const spec = bestDisplay(patternNames(marker))
    if (!spec) continue
    if (spec.extremeOnly && !isNearPriceExtreme(rows, index)) continue
    const above = marker.side === 'bear' || (marker.side === 'neutral' && closerToHigh(rows, index))
    const candidate: Candidate = {
      date,
      index,
      rank: spec.rank,
      label: spec.label,
      side: marker.side,
      above,
    }
    const prev = byDate.get(date)
    if (!prev || candidate.rank > prev.rank) byDate.set(date, candidate)
  }

  const ordered = [...byDate.values()].sort((a, b) => b.rank - a.rank || b.index - a.index)
  const kept: Candidate[] = []
  for (const item of ordered) {
    if (kept.some(other => Math.abs(other.index - item.index) < LABEL_GAP)) continue
    kept.push(item)
  }
  kept.sort((a, b) => a.index - b.index)

  return kept.map(item => ({
    date: item.date,
    kind: item.side === 'bear' ? 'sell' : item.side === 'neutral' ? 'neutral' : 'buy',
    label: item.label,
    patternTag: true,
    above: item.above,
  }))
}
