/**
 * DSA 使用率第 1 步 —— 本地四层漏斗计数契约。
 *
 * 仅供研究，不构成投资建议；执行须人工确认。
 *
 * ## 这是什么
 *
 * 「使用率」在本仓是**部署者自用漏斗诊断**：DSA 导航项有没有被看见、服务有没有连通、
 * 决策页有没有真被打开、打开后有没有做留存动作。它不是多租户增长看板，
 * 也不对外报送（软提醒 2 / 硬约束 6）。
 *
 * ## 四层
 *
 * | 层 | 键 | 语义 | 本拍 |
 * |---|---|---|---|
 * | L1 启用 | `tsp.dsa.funnel.enabled_seen` | 曾见 `status.enabled === true` | 置位 `'1'`，不回退 |
 * | L2 可达 | `tsp.dsa.funnel.reachable_true_count` / `..._false_count` | 每次 status 拉取完成 +1（二选一） | 本地累计 |
 * | L3 打开 | `tsp.dsa.funnel.open_workspace_count` | 决策页 mount +1 | 本地累计 |
 * | L4 留存动作 | `tsp.dsa.funnel.core_action_count` | 核心动作（狙击/研报/问股）计数 | **只留槽**，`design_pending` |
 *
 * ## enabled ≠ reachable（软提醒 1）
 *
 * 后端 `enabled()` 约等于 `bool(DSA_BASE_URL)`（`backend/app/custom/dsa/proxy.py`）。
 * 默认 `.env` 一旦写了 `DSA_BASE_URL`，`enabled` 就**恒真**，而 `reachable` 来自
 * `health()` 的真实探活。所以：
 *
 * - 导航与「服务是否可用」文案**一律以 `reachable` 为准**，本模块的派生函数同理；
 * - **禁止**用 `enabled === true` 冒充「已就绪 / 已连接」。L1 只记录「曾经启用过」，
 *   它是漏斗入口的分母，不是可用性结论。
 *
 * ## pending 与红线
 *
 * - 无生产多日采样 → {@link DSA_FUNNEL_STATUS} = `data_pending`；
 * - L4 动作枚举未钉 → {@link DSA_FUNNEL_L4_STATUS} = `design_pending`；
 * - **禁止**返回或展示「可达率 xx%」「使用率已测通」「打开率已达标」「DSA 正式可推」；
 * - **禁止**上报网络；**禁止**写入 `buy_sell_signals` 的 store/push（不进 BSS 三闸链路）；
 * - 与上游 LLM token 用量（`UsageSummary` / `data-usage-summary`）**分标题、不混名、不互相覆盖**。
 *
 * ## 节流
 *
 * L2 只跟随既有 `refetchInterval: 30_000` 的 status 结果写入，同一份 fetch 结果**只计一次**
 * （由调用方按 `dataUpdatedAt` 去重，见 {@link recordStatusSample}）。禁止另开 `< 30s` 的
 * 紧循环刷 localStorage。
 */

/** localStorage 键前缀。本机 only，不上报（软提醒 7）。 */
export const DSA_FUNNEL_KEY_PREFIX = 'tsp.dsa.funnel.' as const

/** L1：曾见 `status.enabled === true`。值是 `'1'`/缺省，不存计数、不回退。 */
export const DSA_FUNNEL_KEY_ENABLED_SEEN = `${DSA_FUNNEL_KEY_PREFIX}enabled_seen` as const
/** L2：`reachable === true` 的累计次数。 */
export const DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT = `${DSA_FUNNEL_KEY_PREFIX}reachable_true_count` as const
/** L2：`reachable === false` 的累计次数。 */
export const DSA_FUNNEL_KEY_REACHABLE_FALSE_COUNT = `${DSA_FUNNEL_KEY_PREFIX}reachable_false_count` as const
/** L3：决策页 mount 次数。 */
export const DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT = `${DSA_FUNNEL_KEY_PREFIX}open_workspace_count` as const
/** L4 槽：核心留存动作次数。本拍**不**因任何动作自增。 */
export const DSA_FUNNEL_KEY_CORE_ACTION_COUNT = `${DSA_FUNNEL_KEY_PREFIX}core_action_count` as const

/** 全部漏斗键（只读诊断区按此顺序渲染）。 */
export const DSA_FUNNEL_KEYS = [
  DSA_FUNNEL_KEY_ENABLED_SEEN,
  DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT,
  DSA_FUNNEL_KEY_REACHABLE_FALSE_COUNT,
  DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT,
  DSA_FUNNEL_KEY_CORE_ACTION_COUNT,
] as const

export type DsaFunnelKey = typeof DSA_FUNNEL_KEYS[number]

/** 无生产多日采样：一切比率类结论都不成立。 */
export const DSA_FUNNEL_STATUS = 'data_pending' as const
/** L4 动作枚举未钉：只留槽，不实装自增。 */
export const DSA_FUNNEL_L4_STATUS = 'design_pending' as const

/**
 * G1–G3 待办标记。全为 `true` 表示「这一层现在是空的/未定的」，不是「已完成」。
 * 展示时请照 {@link DSA_FUNNEL_PENDING_NOTES} 的原文，勿改写成肯定句。
 */
export const DSA_FUNNEL_PENDING = {
  production_sample_data_pending: true,
  l4_action_enum_design_pending: true,
} as const

/** 四层人读名。顺序即漏斗顺序。 */
export const DSA_FUNNEL_LAYERS = [
  { layer: 'L1', id: 'enabled_seen', label: '启用' },
  { layer: 'L2', id: 'reachable_true_count', label: '可达' },
  { layer: 'L3', id: 'open_workspace_count', label: '打开' },
  { layer: 'L4', id: 'core_action_count', label: '留存动作' },
] as const

/** pending 标签的人读说明（只读诊断区与文档共用同一份措辞）。 */
export const DSA_FUNNEL_PENDING_NOTES: Record<DsaFunnelKey, string> = {
  [DSA_FUNNEL_KEY_ENABLED_SEEN]: '曾见启用标记；不代表服务可用（enabled 可能恒真）',
  [DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT]: '本机累计，跟随 30s 状态轮询；无生产多日采样',
  [DSA_FUNNEL_KEY_REACHABLE_FALSE_COUNT]: '本机累计，跟随 30s 状态轮询；无生产多日采样',
  [DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT]: '本机累计，每次打开决策页 +1',
  [DSA_FUNNEL_KEY_CORE_ACTION_COUNT]: 'design_pending：动作枚举未钉，本拍不自增',
}

/** 本地计数快照。`enabledSeen` 是布尔，其余是次数。 */
export interface DsaFunnelCounts {
  enabledSeen: boolean
  reachableTrueCount: number
  reachableFalseCount: number
  openWorkspaceCount: number
  coreActionCount: number
}

/**
 * 只读诊断区的展示模型。
 *
 * 注意 {@link reachabilityLabel} 只区分「已连接 / 未连接 / 检测中」，
 * **不**给出任何比率；`status` 恒为 `data_pending` 提醒读者「这还不是结论」。
 */
export interface DsaFunnelDiagnostics {
  counts: DsaFunnelCounts
  status: typeof DSA_FUNNEL_STATUS
  l4Status: typeof DSA_FUNNEL_L4_STATUS
  reachabilityLabel: '已连接' | '未连接' | '未检测'
  pending: typeof DSA_FUNNEL_PENDING
}

function storage(): Storage | null {
  try {
    // 隐私模式 / SSR / 单测 node 环境下 localStorage 可能不存在或抛异常
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

function readRaw(key: string): string | null {
  try {
    return storage()?.getItem(key) ?? null
  } catch {
    // 配额满 / 被策略禁用：读失败按「没有」处理，不抛给调用方
    return null
  }
}

function writeRaw(key: string, value: string): void {
  try {
    storage()?.setItem(key, value)
  } catch {
    // 写失败静默：漏斗是诊断附属物，不能因为它把页面打挂
  }
}

/** 解析非负整数计数；坏值一律按 0（不抛、不猜）。 */
function readCount(key: DsaFunnelKey): number {
  const raw = readRaw(key)
  if (raw == null) return 0
  const parsed = Number.parseInt(raw, 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0
}

function writeCount(key: DsaFunnelKey, value: number): void {
  writeRaw(key, String(Math.max(0, Math.trunc(value))))
}

/** 读取本地四层计数快照。任何环境下都返回可用的对象。 */
export function readFunnelCounts(): DsaFunnelCounts {
  return {
    enabledSeen: readRaw(DSA_FUNNEL_KEY_ENABLED_SEEN) === '1',
    reachableTrueCount: readCount(DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT),
    reachableFalseCount: readCount(DSA_FUNNEL_KEY_REACHABLE_FALSE_COUNT),
    openWorkspaceCount: readCount(DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT),
    coreActionCount: readCount(DSA_FUNNEL_KEY_CORE_ACTION_COUNT),
  }
}

/**
 * L1：见过 `enabled === true` 就置位。只置位、不回退 —— 漏斗入口是「曾经到过」，
 * 不是「此刻仍可用」。
 *
 * @returns 本次是否由未置位变为置位（便于调用方判断要不要重渲染）
 */
export function markEnabledSeen(): boolean {
  if (readRaw(DSA_FUNNEL_KEY_ENABLED_SEEN) === '1') return false
  writeRaw(DSA_FUNNEL_KEY_ENABLED_SEEN, '1')
  return true
}

/** L2：`reachable === true` 计数 +1。 */
export function incrementReachableTrue(): number {
  const next = readCount(DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT) + 1
  writeCount(DSA_FUNNEL_KEY_REACHABLE_TRUE_COUNT, next)
  return next
}

/** L2：`reachable === false` 计数 +1。 */
export function incrementReachableFalse(): number {
  const next = readCount(DSA_FUNNEL_KEY_REACHABLE_FALSE_COUNT) + 1
  writeCount(DSA_FUNNEL_KEY_REACHABLE_FALSE_COUNT, next)
  return next
}

/** L3：决策页打开 +1。 */
export function incrementOpenWorkspace(): number {
  const next = readCount(DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT) + 1
  writeCount(DSA_FUNNEL_KEY_OPEN_WORKSPACE_COUNT, next)
  return next
}

/**
 * L4 槽位读写。本拍**没有**任何调用方会让它自增 —— 动作枚举（狙击/研报/问股）
 * 尚未钉死，见 {@link DSA_FUNNEL_L4_STATUS}。保留读写是为了下一步不必改键名。
 */
export function readCoreActionCount(): number {
  return readCount(DSA_FUNNEL_KEY_CORE_ACTION_COUNT)
}

/** 仅供测试与将来的 L4 实装复位；本拍无生产调用方。 */
export function resetCoreActionCount(): void {
  writeCount(DSA_FUNNEL_KEY_CORE_ACTION_COUNT, 0)
}

/** 清空全部漏斗键（只给测试 / 手工排障用）。 */
export function resetFunnelCounts(): void {
  for (const key of DSA_FUNNEL_KEYS) {
    try {
      storage()?.removeItem(key)
    } catch {
      // 同上：清不掉也不影响页面
    }
  }
}

/**
 * 写入一次 status 采样，并返回该次是否为重复采样。
 *
 * - 节流靠调用方传入的 `sampleAt`（TanStack Query 的 `dataUpdatedAt`）：同一份 fetch
 *   结果 `sampleAt` 相同，重复调用只会计一次，从而避免 React 重渲染导致的重复 +1。
 * - 判定**只看 `reachable`**；`enabled` 只用于 L1 置位（软提醒 1）。
 * - `reachable` 非布尔（如字段缺失）时不计入，避免把「不知道」记成「不可达」。
 */
export function recordStatusSample(
  status: { enabled?: boolean; reachable?: boolean } | null | undefined,
  sampleAt: number,
): { counted: boolean; countedAs?: 'true' | 'false' } {
  if (!status) return { counted: false }
  if (status.enabled === true) markEnabledSeen()
  if (typeof status.reachable !== 'boolean') return { counted: false }
  const guardKey = `${DSA_FUNNEL_KEY_PREFIX}last_sample_at`
  if (readRaw(guardKey) === String(sampleAt)) return { counted: false }
  writeRaw(guardKey, String(sampleAt))
  if (status.reachable) {
    incrementReachableTrue()
    return { counted: true, countedAs: 'true' }
  }
  incrementReachableFalse()
  return { counted: true, countedAs: 'false' }
}

/**
 * 构造只读诊断展示模型。
 *
 * `status` 恒为 `data_pending`：本机计数只能说明「本机发生过什么」，
 * 推不出「使用率是多少」。请勿在此处补算比率。
 */
export function buildFunnelDiagnostics(
  status: { reachable?: boolean } | null | undefined,
): DsaFunnelDiagnostics {
  const counts = readFunnelCounts()
  const reachabilityLabel: DsaFunnelDiagnostics['reachabilityLabel'] =
    typeof status?.reachable !== 'boolean'
      ? '未检测'
      : status.reachable ? '已连接' : '未连接'
  return {
    counts,
    status: DSA_FUNNEL_STATUS,
    l4Status: DSA_FUNNEL_L4_STATUS,
    reachabilityLabel,
    pending: DSA_FUNNEL_PENDING,
  }
}
