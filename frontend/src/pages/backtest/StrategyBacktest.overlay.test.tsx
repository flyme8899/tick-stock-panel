// @vitest-environment jsdom
/**
 * 回测页策略选择器: AI 自助生成入口 + 「叠加」多选现有策略。
 *
 * 批次 20261026gbtstrat · 覆盖 G-T1–G-T6 / G-T8 / G-T9 / G-T10 的前端面:
 * - G-T1 AI 分组空态仍可见「创建策略 · AI」入口 (不是只剩「当前分组暂无策略」)
 * - G-T2 生成保存后出现在 AI 分组并可被选中
 * - G-T3 全程无路由跳转到 /screener (不跨选股页)
 * - G-T5 「叠加」tab 列 builtin/custom/ai, 不含 composite_
 * - G-T6 有非 composite 策略时不得只显示「当前分组暂无策略」(禁伪空)
 * - G-T8 多选 >=2 直接当次回测, 无需先存 composite_
 * - G-T9 列表侧过滤 composite; 后端仍拒 (后端测例在 test_backtest_overlay.py)
 * - G-T10 AI 未配置/生成失败的错误可见 (由 Builder 承担, 这里验入口存在且不假绿)
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrategyBacktest } from './StrategyBacktest'

const state = vi.hoisted(() => ({
  presets: [] as any[],
  startBacktestCalls: [] as any[],
  publishCalls: [] as string[],
  navigateCalls: [] as string[],
  saveHandlers: [] as any[],
}))

function preset(id: string, source: string, extra: Record<string, any> = {}) {
  return {
    id,
    name: id,
    description: '',
    tags: [],
    source,
    execution_backend: extra.execution_backend ?? 'matrix_native',
    asset_types: ['stock'],
    timeframes: ['1d'],
    version: '1',
    basic_filter: {},
    params: [],
    params_defaults: {},
    scoring: {},
    scoring_directions: {},
    entry_signals: [],
    exit_signals: [],
    minute_exit_trigger_supported_signals: [],
    stop_loss: null,
    take_profit: null,
    trailing_stop: null,
    trailing_take_profit_activate: null,
    trailing_take_profit_drawdown: null,
    max_hold_days: null,
    ...extra,
  }
}

vi.mock('@/lib/backtestTask', () => ({
  startBacktest: (params: any) => { state.startBacktestCalls.push(params) },
  stopBacktest: () => {},
  tryReconnect: () => false,
  useBacktestTask: () => null,
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<any>('react-router-dom')
  return {
    ...actual,
    useNavigate: () => (to: string) => { state.navigateCalls.push(to) },
  }
})

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<any>('@/lib/api')
  return {
    ...actual,
    api: {
      ...actual.api,
      screenerStrategies: async () => ({ presets: state.presets, load_errors: [] }),
      strategyGet: async (id: string) => state.presets.find(p => p.id === id) ?? state.presets[0],
      strategyPublish: async (id: string) => { state.publishCalls.push(id); return { ok: true, strategy_id: id } },
      dataStatus: async () => ({
        stock: { earliest_date: '2020-01-02', latest_date: '2025-06-30' },
        etf: { earliest_date: '2020-01-02', latest_date: '2025-06-30' },
        enriched: { earliest_date: '2020-01-02', latest_date: '2025-06-30' },
        etf_enriched: { earliest_date: '2020-01-02', latest_date: '2025-06-30' },
      }),
      dataHealth: async () => ({}),
      capabilities: async () => ({ caps: [] }),
      strategyAiStatus: async () => ({ configured: true, has_key: true, has_model: true }),
    },
  }
})

// Builder 太重 (自带 AI 迭代/代码编辑), 这里只验"回测页把它挂上了、参数对、回调闭环"。
vi.mock('@/components/screener/StrategyBuilderDialog', () => ({
  StrategyBuilderDialog: ({ open, onSavedId, mode }: any) => {
    if (open) state.saveHandlers.push(onSavedId)
    return (
      <div data-builder={open ? 'open' : 'closed'} data-builder-mode={mode}>
        <button
          type="button"
          data-builder-save
          data-research-only="false"
          onClick={() => { void onSavedId?.('ai_new_one', false) }}
        />
      </div>
    )
  },
}))

let container: HTMLDivElement
let root: Root

function mount() {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
  act(() => {
    root.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/backtest']}>
          <StrategyBacktest />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
}

async function flush(times = 6) {
  for (let i = 0; i < times; i += 1) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

function groupButton(label: string): HTMLButtonElement {
  const btn = [...container.querySelectorAll('button')].find(b => b.textContent?.trim() === label)
  if (!btn) throw new Error(`找不到分组按钮: ${label}`)
  return btn as HTMLButtonElement
}

async function clickGroup(label: string) {
  await act(async () => { groupButton(label).click() })
  await flush()
}

beforeEach(() => {
  state.presets = [
    preset('ma_golden_cross', 'builtin'),
    preset('my_custom', 'custom'),
    preset('ai_mine', 'ai'),
    preset('composite_old', 'composite', { execution_backend: 'composite' }),
  ]
  state.startBacktestCalls.length = 0
  state.publishCalls.length = 0
  state.navigateCalls.length = 0
  state.saveHandlers.length = 0
  localStorage.clear()
})

afterEach(() => {
  act(() => { root.unmount() })
  container.remove()
})

describe('「叠加」tab 列表语义 (G-T5 / G-T6 / G-T9)', () => {
  it('列出 builtin/custom/ai, 且不含 composite', async () => {
    mount()
    await flush()
    await clickGroup('叠加')

    const ids = [...container.querySelectorAll('[data-strategy-id]')].map(
      el => el.getAttribute('data-strategy-id'),
    )
    expect(ids).toContain('ma_golden_cross')
    expect(ids).toContain('my_custom')
    expect(ids).toContain('ai_mine')
    expect(ids).not.toContain('composite_old')
  })

  it('有可选策略时不得只显示「当前分组暂无策略」(禁伪空)', async () => {
    mount()
    await flush()
    await clickGroup('叠加')

    const text = container.textContent ?? ''
    expect(text).not.toContain('当前分组暂无策略')
    expect(container.querySelectorAll('[data-strategy-id]').length).toBeGreaterThan(0)
  })

  it('策略池只有 composite 时才显示真空态提示', async () => {
    state.presets = [preset('composite_only', 'composite', { execution_backend: 'composite' })]
    mount()
    await flush()
    await clickGroup('叠加')

    expect(container.querySelectorAll('[data-strategy-id]').length).toBe(0)
    expect(container.textContent).toContain('策略池为空')
  })
})

describe('叠加多选与当次回测 (G-T8)', () => {
  it('多选 >=2 后运行回测, 请求带 overlay_children 与默认 intersect', async () => {
    mount()
    await flush()
    await clickGroup('叠加')

    const pick = (id: string) => {
      const el = container.querySelector(`[data-strategy-id="${id}"]`) as HTMLButtonElement
      expect(el).toBeTruthy()
      el.click()
    }
    await act(async () => { pick('ma_golden_cross') })
    await act(async () => { pick('my_custom') })
    await flush()

    const marked = [...container.querySelectorAll('[data-overlay-picked="true"]')].map(
      el => el.getAttribute('data-strategy-id'),
    )
    expect(marked).toEqual(['ma_golden_cross', 'my_custom'])

    const run = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('运行叠加回测'))
    expect(run).toBeTruthy()
    await act(async () => { (run as HTMLButtonElement).click() })
    await flush()

    expect(state.startBacktestCalls).toHaveLength(1)
    const payload = state.startBacktestCalls[0]
    expect(payload.overlay_children).toEqual([
      { strategy_id: 'ma_golden_cross', weight: 1 },
      { strategy_id: 'my_custom', weight: 1 },
    ])
    expect(payload.merge_mode).toBe('intersect')
    expect(payload.strategy_id).toBe('__ephemeral_overlay__')
    // 不强制另存: 没有任何 composite 保存调用
    expect(state.publishCalls).toEqual([])
  })

  it('不足 2 个时运行按钮禁用并给出原因', async () => {
    mount()
    await flush()
    await clickGroup('叠加')

    await act(async () => {
      (container.querySelector('[data-strategy-id="ma_golden_cross"]') as HTMLButtonElement).click()
    })
    await flush()

    const run = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('运行叠加回测')) as HTMLButtonElement
    expect(run).toBeTruthy()
    expect(run.disabled).toBe(true)
    expect(container.textContent).toContain('叠加至少选 2 个策略')
    expect(state.startBacktestCalls).toHaveLength(0)
  })

  it('可切换为并集', async () => {
    mount()
    await flush()
    await clickGroup('叠加')

    for (const id of ['ma_golden_cross', 'my_custom']) {
      await act(async () => {
        (container.querySelector(`[data-strategy-id="${id}"]`) as HTMLButtonElement).click()
      })
    }
    await act(async () => {
      (container.querySelector('[data-overlay-merge-mode="union"]') as HTMLButtonElement).click()
    })
    await flush()

    await act(async () => {
      ([...container.querySelectorAll('button')].find(b => b.textContent?.includes('运行叠加回测')) as HTMLButtonElement).click()
    })
    await flush()
    expect(state.startBacktestCalls[0].merge_mode).toBe('union')
  })
})

describe('AI 自助创建入口 (G-T1 / G-T2 / G-T3)', () => {
  it('AI 分组空态仍显示创建入口', async () => {
    state.presets = [preset('ma_golden_cross', 'builtin')]
    mount()
    await flush()
    await clickGroup('AI')

    const entry = container.querySelector('[data-ai-create-entry]')
    expect(entry).toBeTruthy()
    expect(entry!.textContent).toContain('创建策略 · AI')
    expect(container.textContent).not.toContain('当前分组暂无策略')
  })

  it('点击入口打开回测页内的 Builder (不跳选股页)', async () => {
    mount()
    await flush()
    await clickGroup('AI')

    await act(async () => {
      (container.querySelector('[data-ai-create-entry]') as HTMLButtonElement).click()
    })
    await flush()

    expect(container.querySelector('[data-builder="open"]')).toBeTruthy()
    expect(container.querySelector('[data-builder-mode="create"]')).toBeTruthy()
    expect(state.navigateCalls).toEqual([])
  })

  it('Builder 保存后切到 AI 分组并可选中该策略', async () => {
    mount()
    await flush()
    await clickGroup('AI')

    // 保存前 AI 分组只有 ai_mine
    expect(container.querySelectorAll('[data-strategy-id]')).toHaveLength(1)

    await act(async () => {
      (container.querySelector('[data-ai-create-entry]') as HTMLButtonElement).click()
    })
    await flush()

    // 模拟后端已落盘新策略, 再触发 Builder 的 onSavedId
    state.presets = [...state.presets, preset('ai_new_one', 'ai')]
    await act(async () => {
      (container.querySelector('[data-builder-save]') as HTMLButtonElement).click()
    })
    await flush(10)

    const ids = [...container.querySelectorAll('[data-strategy-id]')].map(
      el => el.getAttribute('data-strategy-id'),
    )
    expect(ids).toContain('ai_new_one')
    expect(state.navigateCalls).toEqual([])
  })
})
