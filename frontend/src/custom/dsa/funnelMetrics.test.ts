// @vitest-environment jsdom
/**
 * 漏斗计数契约测试（批次 20261025gdsa）。
 *
 * 只听契约：键名/前缀、L1–L3 读写、L4 只留槽 + design_pending、pending 标记、
 * 以及「不出现比率/已测通类肯定句」的红线。不测真实 sidecar，不联网。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, expect, it } from 'vitest'
import {
  buildFunnelDiagnostics,
  DSA_FUNNEL_KEY_CORE_ACTION_COUNT,
  DSA_FUNNEL_KEY_ENABLED_SEEN,
  DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT,
  DSA_FUNNEL_KEY_PREFIX,
  DSA_FUNNEL_KEY_REACHABLE_FALSE_COUNT,
  DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT,
  DSA_FUNNEL_KEYS,
  DSA_FUNNEL_L4_STATUS,
  DSA_FUNNEL_LAYERS,
  DSA_FUNNEL_PENDING,
  DSA_FUNNEL_PENDING_NOTES,
  DSA_FUNNEL_STATUS,
  incrementOpenWorkspace,
  incrementReachableFalse,
  incrementReachableTrue,
  markEnabledSeen,
  readCoreActionCount,
  readFunnelCounts,
  recordStatusSample,
  resetCoreActionCount,
  resetFunnelCounts,
} from './funnelMetrics'

beforeEach(() => {
  localStorage.clear()
})

// 收尾也清一次：本文件写的是全局 localStorage，避免残留键串到同环境的其它测试文件
afterEach(() => {
  resetFunnelCounts()
  localStorage.removeItem('tsp.dsa.funnel.last_sample_at')
})

// ── 契约字段可见 ──────────────────────────────────────────────

it('键前缀是 tsp.dsa.funnel.', () => {
  expect(DSA_FUNNEL_KEY_PREFIX).toBe('tsp.dsa.funnel.')
})

it('五个键名与任务书钉死的名字一致', () => {
  expect(DSA_FUNNEL_KEY_ENABLED_SEEN).toBe('tsp.dsa.funnel.enabled_seen')
  expect(DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT).toBe('tsp.dsa.funnel.reachable_true_count')
  expect(DSA_FUNNEL_KEY_REACHABLE_FALSE_COUNT).toBe('tsp.dsa.funnel.reachable_false_count')
  expect(DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT).toBe('tsp.dsa.funnel.open_workspace_count')
  expect(DSA_FUNNEL_KEY_CORE_ACTION_COUNT).toBe('tsp.dsa.funnel.core_action_count')
})

it('DSA_FUNNEL_KEYS 覆盖全部五键，且都带前缀', () => {
  expect(new Set(DSA_FUNNEL_KEYS).size).toBe(5)
  for (const key of DSA_FUNNEL_KEYS) expect(key.startsWith(DSA_FUNNEL_KEY_PREFIX)).toBe(true)
})

it('四层 L1–L4 都有登记（少一层就说明契约被削）', () => {
  expect(DSA_FUNNEL_LAYERS.map(item => item.layer)).toEqual(['L1', 'L2', 'L3', 'L4'])
  expect(DSA_FUNNEL_LAYERS.map(item => item.id)).toEqual([
    'enabled_seen', 'reachable_true_count', 'open_workspace_count', 'core_action_count',
  ])
})

it('pending 标签：数据侧 data_pending，L4 侧 design_pending', () => {
  expect(DSA_FUNNEL_STATUS).toBe('data_pending')
  expect(DSA_FUNNEL_L4_STATUS).toBe('design_pending')
  expect(DSA_FUNNEL_PENDING.production_sample_data_pending).toBe(true)
  expect(DSA_FUNNEL_PENDING.l4_action_enum_design_pending).toBe(true)
})

it('每个键都有 pending 说明，L4 的说明必须点出 design_pending', () => {
  for (const key of DSA_FUNNEL_KEYS) {
    expect(DSA_FUNNEL_PENDING_NOTES[key]).toBeTruthy()
  }
  expect(DSA_FUNNEL_PENDING_NOTES[DSA_FUNNEL_KEY_CORE_ACTION_COUNT]).toContain('design_pending')
})

// ── L1–L3 本地计数 ────────────────────────────────────────────

it('空环境读出全零，且 enabledSeen=false', () => {
  expect(readFunnelCounts()).toEqual({
    enabledSeen: false,
    reachableTrueCount: 0,
    reachableFalseCount: 0,
    openWorkspaceCount: 0,
    coreActionCount: 0,
  })
})

it('L1 只置位一次，不回退', () => {
  expect(readFunnelCounts().enabledSeen).toBe(false)
  expect(markEnabledSeen()).toBe(true)
  expect(readFunnelCounts().enabledSeen).toBe(true)
  // 再调用不再「新置位」
  expect(markEnabledSeen()).toBe(false)
  expect(localStorage.getItem(DSA_FUNNEL_KEY_ENABLED_SEEN)).toBe('1')
})

it('L2 可达通/断各自累加，互不串账', () => {
  expect(incrementReachableTrue()).toBe(1)
  expect(incrementReachableTrue()).toBe(2)
  expect(incrementReachableFalse()).toBe(1)
  const counts = readFunnelCounts()
  expect(counts.reachableTrueCount).toBe(2)
  expect(counts.reachableFalseCount).toBe(1)
})

it('L3 打开计数累加', () => {
  expect(incrementOpenWorkspace()).toBe(1)
  expect(incrementOpenWorkspace()).toBe(2)
  expect(readFunnelCounts().openWorkspaceCount).toBe(2)
})

it('坏值（非数字 / 负数）按 0 处理，不抛异常', () => {
  localStorage.setItem(DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT, 'abc')
  localStorage.setItem(DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT, '-5')
  const counts = readFunnelCounts()
  expect(counts.openWorkspaceCount).toBe(0)
  expect(counts.reachableTrueCount).toBe(0)
})

// ── L4 槽位 ──────────────────────────────────────────────────

it('L4 槽位存在，但本拍没有任何动作会让它自增', () => {
  // 走遍 L1–L3 的全部写入入口
  markEnabledSeen()
  incrementReachableTrue()
  incrementReachableFalse()
  incrementOpenWorkspace()
  recordStatusSample({ enabled: true, reachable: true }, 1)
  recordStatusSample({ enabled: true, reachable: false }, 2)
  expect(readCoreActionCount()).toBe(0)
  expect(readFunnelCounts().coreActionCount).toBe(0)
})

it('L4 槽位可读写（保住键名，下一步不用迁移）', () => {
  resetCoreActionCount()
  expect(readCoreActionCount()).toBe(0)
  localStorage.setItem(DSA_FUNNEL_KEY_CORE_ACTION_COUNT, '7')
  expect(readCoreActionCount()).toBe(7)
})

// ── recordStatusSample 节流 / 去重 ────────────────────────────

it('同一份 fetch 结果只计一次（重复调用 dataUpdatedAt 相同）', () => {
  const first = recordStatusSample({ enabled: true, reachable: true }, 1000)
  expect(first).toEqual({ counted: true, countedAs: 'true' })
  const again = recordStatusSample({ enabled: true, reachable: true }, 1000)
  expect(again.counted).toBe(false)
  expect(readFunnelCounts().reachableTrueCount).toBe(1)
})

it('换一次 fetch（dataUpdatedAt 变了）才 +1', () => {
  recordStatusSample({ enabled: true, reachable: true }, 1000)
  recordStatusSample({ enabled: true, reachable: true }, 2000)
  expect(readFunnelCounts().reachableTrueCount).toBe(2)
})

it('reachable=false 计入 false 侧，且不污染 true 侧', () => {
  recordStatusSample({ enabled: true, reachable: false }, 1000)
  const counts = readFunnelCounts()
  expect(counts.reachableFalseCount).toBe(1)
  expect(counts.reachableTrueCount).toBe(0)
})

it('enabled=true 只置 L1，不因为 enabled 就记成可达', () => {
  recordStatusSample({ enabled: true, reachable: false }, 1000)
  const counts = readFunnelCounts()
  expect(counts.enabledSeen).toBe(true)
  expect(counts.reachableTrueCount).toBe(0)
  expect(counts.reachableFalseCount).toBe(1)
})

it('reachable 缺失（非布尔）时不计入任何一侧 —— 不把「不知道」记成「不可达」', () => {
  const result = recordStatusSample({ enabled: true } as { enabled: boolean }, 1000)
  expect(result.counted).toBe(false)
  const counts = readFunnelCounts()
  expect(counts.reachableTrueCount).toBe(0)
  expect(counts.reachableFalseCount).toBe(0)
  expect(counts.enabledSeen).toBe(true)
})

it('null / undefined 状态不写任何计数', () => {
  expect(recordStatusSample(null, 1000).counted).toBe(false)
  expect(recordStatusSample(undefined, 1000).counted).toBe(false)
  expect(readFunnelCounts().reachableTrueCount + readFunnelCounts().reachableFalseCount).toBe(0)
})

it('resetFunnelCounts 清空全部五键', () => {
  markEnabledSeen()
  incrementReachableTrue()
  incrementReachableFalse()
  incrementOpenWorkspace()
  localStorage.setItem(DSA_FUNNEL_KEY_CORE_ACTION_COUNT, '3')
  resetFunnelCounts()
  for (const key of DSA_FUNNEL_KEYS) expect(localStorage.getItem(key)).toBeNull()
})

// ── 只读诊断模型 ──────────────────────────────────────────────

it('诊断模型：status 恒为 data_pending，不给比率', () => {
  incrementReachableTrue()
  incrementReachableFalse()
  const diag = buildFunnelDiagnostics({ reachable: true })
  expect(diag.status).toBe('data_pending')
  expect(diag.l4Status).toBe('design_pending')
  expect(diag.reachabilityLabel).toBe('已连接')
  expect(diag.counts.reachableTrueCount).toBe(1)
})

it('诊断模型：可达标签只三态，且以 reachable 为准', () => {
  expect(buildFunnelDiagnostics({ reachable: true }).reachabilityLabel).toBe('已连接')
  expect(buildFunnelDiagnostics({ reachable: false }).reachabilityLabel).toBe('未连接')
  expect(buildFunnelDiagnostics(null).reachabilityLabel).toBe('未检测')
  // 只给 enabled 不给 reachable：仍然是「未检测」，不冒充已连接
  expect(buildFunnelDiagnostics({ enabled: true } as { reachable?: boolean }).reachabilityLabel).toBe('未检测')
})

it('诊断模型里没有任何比率字段（结构层防线）', () => {
  const diag = buildFunnelDiagnostics({ reachable: true })
  const keys = Object.keys(diag).join(',')
  for (const banned of ['rate', 'ratio', 'percent', 'pct']) {
    expect(keys.toLowerCase()).not.toContain(banned)
  }
})

// ── 文案红线 ─────────────────────────────────────────────────

const BANNED_CLAIMS = ['可达率', '打开率', '使用率已', '已测通', '已达标', '正式可推']

it('源码里没有「可达率 / 已测通 / 正式可推」这类肯定句', () => {
  const source = readFileSync(resolve(__dirname, 'funnelMetrics.ts'), 'utf8')
  // 只扫字符串字面量里的断言：注释里解释「禁止写 xx」是允许的
  const literals = [...source.matchAll(/'([^'\\\n]*)'|"([^"\\\n]*)"/g)].map(m => m[1] ?? m[2] ?? '')
  const offenders = literals.filter(text => BANNED_CLAIMS.some(word => text.includes(word)))
  expect(offenders).toEqual([])
})

it('本文件不发起网络请求、不写 buy_sell_signals', () => {
  // 只扫代码：注释里说明「不碰 /api/dsa/status」「不进 buy_sell_signals」是允许的，
  // 真正要防的是代码路径。简单剥掉块注释与行注释即可（本文件无字符串内含 // 的情况）。
  const source = readFileSync(resolve(__dirname, 'funnelMetrics.ts'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1')
  for (const banned of ['fetch(', 'XMLHttpRequest', 'navigator.sendBeacon', 'buy_sell_signals', '/api/']) {
    expect(source).not.toContain(banned)
  }
})
