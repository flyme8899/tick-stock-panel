// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { EChartsCandlestick, type OHLC } from './EChartsCandlestick'

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: any) => void>,
  setOption: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
  dispatchAction: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
  getOption: () => ({ dataZoom: [{ start: 0, end: 100 }] }),
  containPixel: () => false,
  convertFromPixel: () => [0, 0],
}))

vi.mock('echarts', () => ({ init: () => chart }))

// 超过 COMPACT_THRESHOLD(60): 切标的后 dataZoom 一响就会走紧凑态转换, 进而重写 markPoint
const DAYS = Array.from({ length: 62 }, (_, i) =>
  new Date(Date.UTC(2024, 0, 1 + i)).toISOString().slice(0, 10))
const rows = (base: number): OHLC[] =>
  DAYS.map(date => ({ date, open: base, high: base + 1, low: base - 1, close: base, volume: 1 }))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.handlers = {}
  chart.setOption.mockClear()
  chart.on.mockImplementation((name: string, callback: (event?: any) => void) => {
    chart.handlers[name] = callback
  })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

/** 最后一次 setOption 里 K 线的 markPoint (买卖箭头来源) */
function lastMarkPoint(): string {
  const option = chart.setOption.mock.calls.at(-1)?.[0] as any
  const k = option?.series?.find((s: any) => s.name === 'K')
  return JSON.stringify(k?.markPoint ?? null)
}

/** 模拟竖虚线命中某根 K 线 (鼠标在数据区内移动时 echarts 派发的事件) */
async function hoverCandle(index: number) {
  await act(async () => { chart.handlers.updateAxisPointer?.({ axesInfo: [{ value: index }] }) })
}

it('切标的后响应 dataZoom 仍用当前标的的买卖标记, 不回退到上一只', async () => {
  const render = async (symbol: string, data: OHLC[], markerLabel: string, markerDate: string) => {
    await act(async () => root.render(
      <EChartsCandlestick
        data={data}
        markers={[{ date: markerDate, kind: 'buy', label: markerLabel }]}
        symbol={symbol}
        height={400}
        showInfoBar={false}
        visibleBars="all"
        activeIndicators={['vol']}
      />,
    ))
  }

  await render('600000', rows(10), 'A-BUY', DAYS[10])
  await render('000001', rows(100), 'B-SELL', DAYS[20])

  // 切换标的后图表实例被复用, dataZoom 监听仍是最初注册的那个; 首次 setOption 已是当前标的
  expect(lastMarkPoint()).toContain('B-SELL')

  await act(async () => { chart.handlers.dataZoom?.() })

  expect(lastMarkPoint()).toContain('B-SELL')
  expect(lastMarkPoint()).not.toContain('A-BUY')
})

it('切股后鼠标未离开图表, 竖虚线重新命中即恢复「至今/周期」', async () => {
  const render = async (symbol: string) => {
    await act(async () => root.render(
      <EChartsCandlestick data={rows(10)} symbol={symbol} height={400} visibleBars="all" />,
    ))
  }

  await render('600000')
  const surface = host.firstElementChild as HTMLElement
  await act(async () => { surface.dispatchEvent(new MouseEvent('mouseenter')) })
  await hoverCandle(30)
  expect(host.textContent).toContain('至今')

  // 切股: 上一只的悬停上下文作废, 清掉「至今/周期」
  await render('000001')
  expect(host.textContent).not.toContain('至今')

  // 鼠标没离开图表区 (只是切股), 竖虚线重新命中即恢复, 不靠 mouseenter
  await hoverCandle(20)
  expect(host.textContent).toContain('至今')
  expect(host.textContent).toContain('周期')

  // 移出图表区 → 重新收起
  await act(async () => { surface.dispatchEvent(new MouseEvent('mouseleave')) })
  expect(host.textContent).not.toContain('至今')
})

it('买卖点用三角、菱形和圆点，旧箭头标记保持不变', async () => {
  const hover = vi.fn()
  await act(async () => root.render(
    <EChartsCandlestick
      data={rows(10)}
      height={400}
      showInfoBar={false}
      visibleBars="all"
      activeIndicators={[]}
      onMarkerHover={hover}
      markers={[
        { date: DAYS[10], kind: 'buy', label: 'OLD' },
        { date: DAYS[11], kind: 'buy', style: 'triangle', markerId: 'b1', color: '#12B76A' },
        { date: DAYS[12], kind: 'buy', style: 'breakout', markerId: 'b2' },
        { date: DAYS[13], kind: 'sell', style: 'triangle', markerId: 's1' },
        { date: DAYS[14], kind: 'buy', style: 'circle', markerId: 'f1' },
      ]}
    />,
  ))
  const option = chart.setOption.mock.calls.at(-1)?.[0] as any
  const points = option.series.find((series: any) => series.name === 'K').markPoint.data
  expect(points.find((point: any) => point.name === 'OLD').symbol).toBe('arrow')
  const buy = points.find((point: any) => point.markerId === 'b1')
  expect(buy.symbol).toBe('triangle')
  expect(buy.symbolRotate).toBe(0)
  expect(buy.itemStyle.color).toBe('#12B76A')
  expect(buy.coord[1]).toBe(rows(10)[11].low)
  const breakout = points.find((point: any) => point.markerId === 'b2')
  expect(breakout.symbol).toBe('diamond')
  const sell = points.find((point: any) => point.markerId === 's1')
  expect(sell.symbol).toBe('triangle')
  expect(sell.symbolRotate).toBe(180)
  expect(sell.itemStyle.color).toBe('#F04438')
  expect(sell.coord[1]).toBe(rows(10)[13].high)
  const fill = points.find((point: any) => point.markerId === 'f1')
  expect(fill.symbol).toBe('circle')
  expect(fill.label.formatter).toBe('B')

  await act(async () => {
    chart.handlers.mouseover?.({
      componentType: 'markPoint',
      data: { markerId: 'b1' },
      event: { offsetX: 12, offsetY: 20 },
    })
  })
  expect(hover).toHaveBeenCalledWith('b1', { x: 12, y: 20 })
  await act(async () => {
    chart.handlers.mouseout?.({ componentType: 'markPoint', data: { markerId: 'b1' } })
  })
  expect(hover).toHaveBeenCalledWith(null)
})
