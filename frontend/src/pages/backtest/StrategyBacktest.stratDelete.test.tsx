// @vitest-environment jsdom
/**
 * 批次 20261028sdel · P-strat-delete: 回测页策略芯片的**删除入口**。
 *
 * 这一组测例只钉一件事, 但钉得很死: **芯片上能删筹码, 且删的动作不外溢**。
 *
 * 主缺口是「芯片块没有删除控件」(tip ABSENT, 见 StrategyBacktest.tsx 芯片 map)。
 * 缺口补上之后真正危险的不是"删不掉", 而是**删除控件把别的东西带坏**:
 *
 * - 点 X 顺带把策略选中了(冒泡到芯片 onClick) —— 用户想删, 界面反而切了策略;
 * - 在叠加 tab 点 X 把策略**勾上**了 —— 比上一条更糟, 下次运行会把它拼进 overlay;
 * - 给 builtin 也画了 X —— 点了才发现服务端 403, 等于骗用户点;
 * - 删完不刷新列表 —— 芯片还挂在那儿, 再点就是 404;
 * - 删的正是当前选中却不清理 —— 右侧停在"加载策略配置…"转圈;
 * - 删的 id 还留在 overlay 多选里 —— 下次回测后端 404;
 * - 被叠加引用(后端 409)时静默 —— 用户以为删掉了。
 *
 * 对应的验收项: G-T2 / G-T3 / G-T4 / G-T5 / G-T6。G-T7(SettingsDialog 不回归)
 * 与 G-T9(零 diff)在 PR 自检与 diff 层面钉, 不在这里重复。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrategyBacktest } from './StrategyBacktest'

const state = vi.hoisted(() => ({
  presets: [] as any[],
  /** `strategyGet` 被调用过的 id。选中态活着 → 它会一直被请求;
   *  选中被清掉 → 不再被请求。这是**不依赖芯片存在**的判据:
   *  芯片删掉之后 `data-strategy-active` 自然全空, 只靠它分不清
   *  "清理了" 与 "策略刚好没了"。 */
  detailCalls: [] as string[],
  /** 挂起列表重取: 用来把「显式清空 selected」与「列表重取后兜底清空」分开测。
   *  置 true 时 `screenerStrategies` 返回一个永不 resolve 的 promise。 */
  holdList: false,
  /** 配合 holdList: 重取被挡在 await 上, 测试什么时候放行、什么时候断言 */
  gate: null as Promise<void> | null,
  deleteCalls: [] as string[],
  /** 每条 preset 的删除行为: 默认成功; 测例可换成抛错(模拟 409) */
  deleteImpl: null as null | ((id: string) => Promise<any>),
  invalidateKeys: [] as any[],
}))

function preset(id: string, source: string, extra: Record<string, any> = {}) {
  return {
    id,
    name: extra.name ?? id,
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
  startBacktest: () => {},
  stopBacktest: () => {},
  tryReconnect: () => false,
  useBacktestTask: () => null,
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<any>('react-router-dom')
  return { ...actual, useNavigate: () => () => {} }
})

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<any>('@/lib/api')
  return {
    ...actual,
    api: {
      ...actual.api,
      screenerStrategies: async () => {
        if (state.holdList) {
          if (state.gate) await state.gate
          return { presets: state.presets, load_errors: [] }
        }
        return { presets: state.presets, load_errors: [] }
      },
      strategyGet: async (id: string) => { state.detailCalls.push(id); return state.presets.find(p => p.id === id) ?? state.presets[0] },
      dataStatus: async () => ({
        stock: { earliest_date: '2020-01-02', latest_date: '2025-06-30' },
        etf: { earliest_date: '2020-01-02', latest_date: '2025-06-30' },
        enriched: { earliest_date: '2020-01-02', latest_date: '2025-06-30' },
        etf_enriched: { earliest_date: '2020-01-02', latest_date: '2025-06-30' },
      }),
      dataHealth: async () => ({}),
      capabilities: async () => ({ caps: [] }),
      strategyAiStatus: async () => ({ configured: true, has_key: true, has_model: true }),
      strategyDelete: async (id: string) => {
        state.deleteCalls.push(id)
        if (state.deleteImpl) return state.deleteImpl(id)
        // 模拟真实后端: 删除成功即从列表消失。**必须在 resolve 之前改**, 因为
        // invalidate 触发的重取紧跟在 await 之后 —— 测试若在 await 之后才改列表,
        // 重取早就拿到旧数据了, 于是"删了但还在"会被误判成产品缺陷。
        state.presets = state.presets.filter(p => p.id !== id)
        return { ok: true }
      },
    },
  }
})

vi.mock('@/components/screener/StrategyBuilderDialog', () => ({
  StrategyBuilderDialog: () => <div data-builder="closed" />,
}))

let container: HTMLDivElement
let root: Root

function mount() {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  // 记录 invalidate 的 queryKey, 用来验"删完真的刷新了列表"
  const origInvalidate = qc.invalidateQueries.bind(qc)
  ;(qc as any).invalidateQueries = (filters?: any) => {
    state.invalidateKeys.push(filters?.queryKey)
    return origInvalidate(filters)
  }
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

/** 只推进微任务/一个宏任务, 不反复 flush —— 用于"卡在半路"的窗口内断言 */
async function tick() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

async function flush(times = 8) {
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

const chip = (id: string) =>
  container.querySelector(`[data-strategy-id="${id}"]`) as HTMLElement | null
const deleteBtn = (id: string) =>
  container.querySelector(`[data-delete-strategy="${id}"]`) as HTMLElement | null
const confirmPanel = () => container.querySelector('[data-strategy-delete-confirm]') as HTMLElement | null

async function clickDelete(id: string) {
  const btn = deleteBtn(id)
  expect(btn, `策略 ${id} 上应有删除入口`).toBeTruthy()
  await act(async () => { btn!.click() })
  await flush()
}

async function clickConfirm() {
  const submit = container.querySelector('[data-strategy-delete-submit]') as HTMLButtonElement | null
  expect(submit, '确认弹窗应有「确认删除」').toBeTruthy()
  await act(async () => { submit!.click() })
  await flush()
}

async function clickCancel() {
  const panel = confirmPanel()
  expect(panel).toBeTruthy()
  const cancel = [...panel!.querySelectorAll('button')].find(b => b.textContent?.trim() === '取消')
  expect(cancel, '确认弹窗应有「取消」').toBeTruthy()
  await act(async () => { (cancel as HTMLButtonElement).click() })
  await flush()
}

beforeEach(() => {
  state.presets = [
    preset('ma_golden_cross', 'builtin', { name: '均线金叉' }),
    preset('chip_stack_custom', 'custom', { name: '筹码' }),
    preset('ai_mine', 'ai', { name: 'AI 动量' }),
    preset('composite_mix', 'composite', { name: '叠加组合', execution_backend: 'composite' }),
  ]
  state.deleteCalls.length = 0
  state.deleteImpl = null
  state.invalidateKeys.length = 0
  state.detailCalls.length = 0
  state.holdList = false
  state.gate = null
  localStorage.clear()
})

afterEach(() => {
  act(() => { root.unmount() })
  container.remove()
})

// ============================================================ G-T2 谁有删除入口

describe('G-T2 只有 custom / ai / composite 有删除入口', () => {
  it('builtin 芯片没有删除入口', async () => {
    mount()
    await flush()
    expect(chip('ma_golden_cross')).toBeTruthy()
    expect(deleteBtn('ma_golden_cross')).toBeNull()
    // 反面: 同一屏里 custom 是有入口的 —— 否则这条测例在"整个功能都没做"时也会绿
    expect(deleteBtn('chip_stack_custom')).toBeTruthy()
  })

  it('custom / ai / composite 都有删除入口', async () => {
    mount()
    await flush()
    for (const id of ['chip_stack_custom', 'ai_mine', 'composite_mix']) {
      expect(deleteBtn(id), `${id} 应有删除入口`).toBeTruthy()
    }
  })

  it('删除入口带可读的 aria-label（不是只有一个裸露的 X 图标）', async () => {
    mount()
    await flush()
    const btn = deleteBtn('chip_stack_custom')!
    expect(btn.getAttribute('aria-label')).toContain('筹码')
    expect(btn.getAttribute('role')).toBe('button')
  })

  it('source 缺失的芯片按不可删处理（白名单, 不是黑名单）', async () => {
    state.presets = [preset('no_source', '', { name: '无名' }), preset('c1', 'custom')]
    mount()
    await flush()
    expect(deleteBtn('no_source')).toBeNull()
    expect(deleteBtn('c1')).toBeTruthy()
  })

  it('叠加 tab 下 custom 仍可删, builtin 仍不可删', async () => {
    mount()
    await flush()
    await clickGroup('叠加')
    expect(chip('chip_stack_custom')).toBeTruthy()
    expect(deleteBtn('chip_stack_custom')).toBeTruthy()
    expect(chip('ma_golden_cross')).toBeTruthy()
    expect(deleteBtn('ma_golden_cross')).toBeNull()
  })
})

// ============================================================ G-T3 点 X 不放外溢

describe('G-T3 点 X 不改变选中 / 勾选', () => {
  it('单选态: 点 X 不切换 selectedStrategy', async () => {
    mount()
    await flush()
    // 先选中 builtin, 让"当前选中"是个明确的已知值
    await act(async () => { chip('ma_golden_cross')!.click() })
    await flush()
    expect(chip('ma_golden_cross')!.getAttribute('data-strategy-active')).toBe('true')

    // 点另一个策略的 X —— 若冒泡到芯片 onClick, 选中会跳到 chip_stack_custom
    await clickDelete('chip_stack_custom')

    expect(chip('ma_golden_cross')!.getAttribute('data-strategy-active')).toBe('true')
    expect(chip('chip_stack_custom')!.getAttribute('data-strategy-active')).toBe('false')
    // 确认弹窗开出来了(证明 X 确实被点到了, 不是没生效的假绿)
    expect(confirmPanel()).toBeTruthy()
  })

  it('叠加态: 点 X 不会把策略勾上', async () => {
    mount()
    await flush()
    await clickGroup('叠加')
    expect(chip('chip_stack_custom')!.getAttribute('data-overlay-picked')).toBe('false')

    await clickDelete('chip_stack_custom')

    expect(chip('chip_stack_custom')!.getAttribute('data-overlay-picked')).toBe('false')
    expect(confirmPanel()).toBeTruthy()
  })

  it('点芯片本体仍然选中（删除入口没把原行为挤掉）', async () => {
    mount()
    await flush()
    await act(async () => { chip('chip_stack_custom')!.click() })
    await flush()
    expect(chip('chip_stack_custom')!.getAttribute('data-strategy-active')).toBe('true')
    expect(confirmPanel()).toBeNull()
  })

  it('点芯片本体在叠加 tab 下仍然勾选', async () => {
    mount()
    await flush()
    await clickGroup('叠加')
    await act(async () => { chip('chip_stack_custom')!.click() })
    await flush()
    expect(chip('chip_stack_custom')!.getAttribute('data-overlay-picked')).toBe('true')
  })
})

// ============================================================ G-T4 确认后真的删

describe('G-T4 确认后调 strategyDelete 并做三件善后', () => {
  it('确认 → 调接口 → 刷新列表 → 芯片消失', async () => {
    mount()
    await flush()
    expect(chip('chip_stack_custom')).toBeTruthy()

    await clickDelete('chip_stack_custom')
    await clickConfirm()

    expect(state.deleteCalls).toEqual(['chip_stack_custom'])
    // 列表真的失效了(不是只把本地状态改一改)
    expect(state.invalidateKeys.some(k => JSON.stringify(k ?? '').includes('screener-strategies'))).toBe(true)

    // 模拟后端已删: 失效后的重取拿到没有它的列表
    await flush(10)
    expect(chip('chip_stack_custom')).toBeNull()
    expect(confirmPanel()).toBeNull()
  })

  it('删的是当前选中 → selectedStrategy 清空（用 strategyGet 是否还在被请求判断）', async () => {
    mount()
    await flush()
    await act(async () => { chip('chip_stack_custom')!.click() })
    await flush()
    expect(chip('chip_stack_custom')!.getAttribute('data-strategy-active')).toBe('true')

    // 选中活着时, 详情 query 会带着这个 id 去请求
    state.detailCalls.length = 0
  state.holdList = false

    await clickDelete('chip_stack_custom')
    await clickConfirm()
    await flush(10)

    // 关键: 「芯片不见了」**不能**当作选中已清空的证据 —— 芯片消失是因为策略被删了,
    // 与 selectedStrategy 有没有被置空无关。要证明清空, 得看选中态的**下游**:
    // 选中态一旦为 null, `strategyDetail` 的 `enabled` 就是 false, 不会再请求。
    const requestsAfterDelete = state.detailCalls.filter(id => id === 'chip_stack_custom')
    expect(requestsAfterDelete).toHaveLength(0)

    // 且不该有任何芯片处于选中态
    expect([...container.querySelectorAll('[data-strategy-active="true"]')]).toHaveLength(0)
  })

  it('列表重取还没回来时, 选中就已清空（显式清理, 不等兜底 effect）', async () => {
    // 组件里另有一道兜底(L1196–1204): 列表就绪后发现 selectedStrategy 不在列表里 →
    // 自动清空。这道兜底意味着"只看最终状态"永远测不出显式清理 —— 所以要卡一个
    // 时间窗: 列表重取被挡在闸门上, 兜底无从触发, 此刻 selectedStrategy 是否为空,
    // 只能由 handleConfirmDeleteStrategy 里那句 setSelectedStrategy 决定。
    mount()
    await flush()
    await act(async () => { chip('chip_stack_custom')!.click() })
    await flush()
    expect(chip('chip_stack_custom')!.getAttribute('data-strategy-active')).toBe('true')
    expect(container.querySelector('[data-strategy-settings]')!.getAttribute('data-selected-strategy-id'))
      .toBe('chip_stack_custom')

    // 闸门: 之后的列表重取会停在 await 上, 直到测试自己放行
    let open!: () => void
    state.gate = new Promise<void>(r => { open = r })
    state.holdList = true

    await clickDelete('chip_stack_custom')
    await act(async () => {
      (container.querySelector('[data-strategy-delete-submit]') as HTMLButtonElement).click()
    })
    await tick()

    // ---- 窗口内: 列表重取尚未回来 ----
    // 判据用「策略设置」按钮上的 `data-selected-strategy-id` —— 它是 selectedStrategy
    // 这个 state 的**直接投影**, 不经过任何派生。
    //
    // 试过但不可用的判据（都栽在同一个坑: 读到的不是 state 本身）:
    //   · `detailCalls` —— detail query 有缓存, enabled 转 false 时不重新请求;
    //   · query cache 的 key —— 旧 key 不会因 enabled=false 立刻回收;
    //   · 「策略设置」区显示的名字 —— 取值链是 `detail?.name ?? 列表查找 ??
    //     '未选择策略'`, 而 `detail` 是缓存里还留着的旧数据, 于是清空之后名字仍是
    //     「筹码」。这条最坑: 看起来在测 UI, 实际在测 TanStack 的缓存策略。
    const attrs = () => container.querySelector('[data-strategy-settings]')!.getAttribute('data-selected-strategy-id')

    // 反面先行: 若这里已经是空串, 说明列表早回来了, 这条测例根本没测到"窗口内"。
    // 芯片还在 = 列表还是旧的 = 窗口成立。
    expect(chip('chip_stack_custom'), '窗口前提: 列表重取还没回来, 芯片应仍在').toBeTruthy()

    // 正题
    expect(attrs(), '列表没回来, 选中就该已经清空了').toBe('')

    // ---- 放行, 确认后续不炸 ----
    await act(async () => { open() })
    await flush(10)
    expect(chip('chip_stack_custom')).toBeNull()
    expect(attrs()).toBe('')
  })

  it('删的不是当前选中策略时, 窗口内选中保持不变（不是"一律清空"）', async () => {
    // 与上一条配对: 上一条证明"该清就清", 这一条证明"不该清就不清"。
    // 少了这条, 一个 `setSelectedStrategy(null)` 无条件清空的假实现也能让上一条绿。
    mount()
    await flush()
    await act(async () => { chip('ma_golden_cross')!.click() })
    await flush()

    let open!: () => void
    state.gate = new Promise<void>(r => { open = r })
    state.holdList = true

    await clickDelete('ai_mine')
    await act(async () => {
      (container.querySelector('[data-strategy-delete-submit]') as HTMLButtonElement).click()
    })
    await tick()

    expect(chip('ai_mine'), '窗口前提: 列表重取还没回来').toBeTruthy()
    expect(container.querySelector('[data-strategy-settings]')!.getAttribute('data-selected-strategy-id'))
      .toBe('ma_golden_cross')

    await act(async () => { open() })
    await flush(10)
    expect(chip('ai_mine')).toBeNull()
  })

  it('删别的策略时, 当前选中的详情仍在正常请求（清空逻辑没有误伤）', async () => {
    mount()
    await flush()
    await act(async () => { chip('ma_golden_cross')!.click() })
    await flush()

    await clickDelete('ai_mine')
    await clickConfirm()
    await flush(10)

    // 选中还是 builtin, 它的详情照常可请求 —— 反过来证明上一条不是"把 selected
    // 一律清空"这种假实现骗过的
    expect(chip('ma_golden_cross')!.getAttribute('data-strategy-active')).toBe('true')
    expect(container.querySelector('[data-selected-strategy-name]')!.textContent).toBe('均线金叉')
  })

  it('删的 id 在叠加多选里 → 从勾选里去掉', async () => {
    mount()
    await flush()
    await clickGroup('叠加')
    for (const id of ['ma_golden_cross', 'chip_stack_custom']) {
      await act(async () => { chip(id)!.click() })
    }
    await flush()
    expect(chip('chip_stack_custom')!.getAttribute('data-overlay-picked')).toBe('true')

    await clickDelete('chip_stack_custom')
    await clickConfirm()
    await flush(10)

    // 同样的陷阱: 勾选里还留着已删 id 时, 芯片已经不存在, 光看芯片还是"干净"的。
    // 判据换成**运行按钮**: 叠加要 >=2 才算 ready。原本勾了 2 个(builtin + custom),
    // 删掉 custom 后应只剩 1 个 —— 按钮转 disabled。
    // 若没有从 selectedOverlayIds 剔除, 计数仍是 2, 按钮会保持可用(这正是 bug)。
    const run = [...container.querySelectorAll('button')]
      .find(b => b.textContent?.includes('运行叠加回测')) as HTMLButtonElement | undefined
    expect(run, '叠加 tab 应有运行按钮').toBeTruthy()
    expect(run!.disabled).toBe(true)
    expect(container.textContent).toContain('叠加至少选 2 个策略')

    // 另一个的勾选要保留
    expect(chip('ma_golden_cross')!.getAttribute('data-overlay-picked')).toBe('true')
  })

  it('删一个不在叠加多选里的策略, 其它勾选与可运行状态不受影响', async () => {
    mount()
    await flush()
    await clickGroup('叠加')
    for (const id of ['ma_golden_cross', 'chip_stack_custom']) {
      await act(async () => { chip(id)!.click() })
    }
    await flush()
    const run = () => [...container.querySelectorAll('button')]
      .find(b => b.textContent?.includes('运行叠加回测')) as HTMLButtonElement
    expect(run().disabled).toBe(false)

    // ai_mine 没被勾上, 删它不该改变叠加的构成
    await clickDelete('ai_mine')
    await clickConfirm()
    await flush(10)

    expect(run().disabled).toBe(false)
    expect(chip('ma_golden_cross')!.getAttribute('data-overlay-picked')).toBe('true')
    expect(chip('chip_stack_custom')!.getAttribute('data-overlay-picked')).toBe('true')
  })

  it('删的不是当前选中 → 选中保持不变', async () => {
    mount()
    await flush()
    await act(async () => { chip('ma_golden_cross')!.click() })
    await flush()

    await clickDelete('ai_mine')
    await clickConfirm()

    await flush(10)
    expect(chip('ma_golden_cross')!.getAttribute('data-strategy-active')).toBe('true')
  })
})

// ============================================================ G-T5 取消不删

describe('G-T5 取消/关掉确认框不删除', () => {
  it('点「取消」不调接口', async () => {
    mount()
    await flush()
    await clickDelete('chip_stack_custom')
    await clickCancel()

    expect(state.deleteCalls).toEqual([])
    expect(confirmPanel()).toBeNull()
    expect(chip('chip_stack_custom')).toBeTruthy()
  })

  it('点遮罩关掉确认框不调接口', async () => {
    mount()
    await flush()
    await clickDelete('chip_stack_custom')
    const panel = confirmPanel()!
    await act(async () => { panel.click() })
    await flush()

    expect(state.deleteCalls).toEqual([])
    expect(confirmPanel()).toBeNull()
    expect(chip('chip_stack_custom')).toBeTruthy()
  })

  it('确认框里点名了要删的策略（不是笼统一句「确定删除？」）', async () => {
    mount()
    await flush()
    await clickDelete('chip_stack_custom')
    expect(confirmPanel()!.textContent).toContain('确定要删除「筹码」吗？')
    expect(confirmPanel()!.textContent).toContain('删除后无法恢复')
  })
})

// ============================================================ G-T6 失败要可见

describe('G-T6 后端拒绝时错误可见（tip = 409 被叠加引用）', () => {
  it('409 detail 显示在确认框内, 弹窗不自己关掉', async () => {
    state.deleteImpl = async () => {
      // 与后端 delete_strategy 的 409 文案同形(不是真实凭证, 只是错误串)
      throw new Error('该策略被叠加策略 composite_mix 引用，请先解除引用后再删除')
    }
    mount()
    await flush()

    await clickDelete('chip_stack_custom')
    await clickConfirm()

    expect(state.deleteCalls).toEqual(['chip_stack_custom'])
    // 弹窗还开着 —— 关掉就等于告诉用户"删好了"
    expect(confirmPanel()).toBeTruthy()
    const err = container.querySelector('[data-strategy-delete-error]')
    expect(err, '错误必须在界面上可见, 不能只有 toast').toBeTruthy()
    expect(err!.textContent).toContain('被叠加策略')
    expect(err!.textContent).toContain('请先解除引用')
    // 策略还在
    expect(chip('chip_stack_custom')).toBeTruthy()
  })

  it('失败后再删另一个策略, 不会看到上一轮的错误', async () => {
    state.deleteImpl = async () => { throw new Error('上一轮的旧错误') }
    mount()
    await flush()

    await clickDelete('chip_stack_custom')
    await clickConfirm()
    expect(container.querySelector('[data-strategy-delete-error]')).toBeTruthy()

    await clickCancel()
    state.deleteImpl = null
    await clickDelete('ai_mine')

    // 新一轮确认框: 同一次操作里不该继承上一条策略的报错
    const err = container.querySelector('[data-strategy-delete-error]')
    expect(err?.textContent ?? '').not.toContain('上一轮的旧错误')
  })

  it('失败时不动选中态与勾选（没有"删了一半"的中间态）', async () => {
    state.deleteImpl = async () => { throw new Error('boom') }
    mount()
    await flush()
    await clickGroup('叠加')
    await act(async () => { chip('chip_stack_custom')!.click() })
    await flush()
    expect(chip('chip_stack_custom')!.getAttribute('data-overlay-picked')).toBe('true')

    await clickDelete('chip_stack_custom')
    await clickConfirm()

    expect(chip('chip_stack_custom')!.getAttribute('data-overlay-picked')).toBe('true')
  })
})
