/**
 * 导航「决策」项的**服务可达状态点**。
 *
 * 仅供研究，不构成投资建议；执行须人工确认。
 *
 * ## 为什么需要它
 *
 * 侧栏原本只给「决策」挂了一个静态 `badge: 'AI'` 字符串。用户在 DSA 服务没起来时
 * 点进去，只看到一页「看起来坏了」的界面 —— 而侧栏没有任何预警。
 *
 * ## 判定只有一条：`reachable`
 *
 * 后端 `GET /api/dsa/status` 返回 `{enabled, base_url, reachable, detail}`。其中
 * `enabled()` 约等于 `bool(DSA_BASE_URL)`，默认 `.env` 下**近乎恒真**；只有
 * `reachable` 来自真实探活 `health()`。因此：
 *
 * - **禁止**在 `enabled === true` 时把状态点画成「已就绪 / 已连接」；
 * - `reachable === true` → 已连接（复用页内徽标的 `bear` token）；
 * - `reachable === false` → 未连接（`warning`），`title` 悬停展示 `detail`；
 * - 尚未拿到结果（loading / 未开通） → 中性「检测中」，不给结论。
 *
 * ## 配色说明
 *
 * 本仓是红涨绿跌。这里用的 `bear` / `warning` 是**服务可达性**语义（与
 * `DecisionWorkspace` 页内徽标完全一致），**不是**涨跌色，勿按涨跌理解。
 *
 * ## 隔离
 *
 * 本组件由扩展注册表注入（`extension.tsx` → `navigation[].accessory`），
 * `Layout.tsx` 只按 `ComponentType` 渲染，**不 import** 本文件或 `@/custom/dsa`。
 */

import { useQuery } from '@tanstack/react-query'
import { QK } from '@/lib/queryKeys'
import { fetchDsaStatus } from './client'

const DOT_BASE = 'ml-auto inline-block h-1.5 w-1.5 shrink-0 rounded-full'

/** 悬停提示：优先展示后端 `detail`，没有就给一句固定说明。 */
function tooltipFor(reachable: boolean | null, detail: string): string | undefined {
  const trimmed = detail.trim()
  if (trimmed) return trimmed
  if (reachable === true) return 'DSA 服务已连接'
  if (reachable === false) return 'DSA 服务未连接'
  return undefined
}

export function DsaNavReachableAccessory() {
  // 复用 QK.dsaStatus：与页内 DecisionWorkspace 命中同一份缓存，不会各自多打一次 /status
  const status = useQuery({
    queryKey: QK.dsaStatus,
    queryFn: fetchDsaStatus,
    refetchInterval: 30_000,
    retry: false,
  })

  // 三态：null 表示「还不知道」。注意这里**不看** enabled。
  const reachable: boolean | null = typeof status.data?.reachable === 'boolean'
    ? status.data.reachable
    : null
  const detail = status.data?.detail ?? ''
  const label = reachable === true ? '已连接' : reachable === false ? '未连接' : '检测中'
  const tone = reachable === true
    ? 'bg-bear'
    : reachable === false
      ? 'bg-warning'
      : 'bg-muted/60'

  return (
    <span
      data-dsa-nav-reachable={reachable === null ? 'unknown' : String(reachable)}
      data-dsa-nav-reachable-label={label}
      title={tooltipFor(reachable, detail)}
      aria-label={`DSA 服务${label}`}
      className="ml-auto inline-flex shrink-0 items-center"
    >
      <span className={`${DOT_BASE} ${tone}`} />
      <span className="sr-only">{label}</span>
    </span>
  )
}

export default DsaNavReachableAccessory
