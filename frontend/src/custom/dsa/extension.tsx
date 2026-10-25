/**
 * DSA 的前端扩展注册表 —— 路由、导航项、以及两个通用插槽。
 *
 * 仅供研究，不构成投资建议；执行须人工确认。
 *
 * ## 本文件守的边界
 *
 * 核心包（components / pages / lib / extensions）**不 import 本文件**。
 * 一切接入都走 `@/extensions/types` 的通用 API（`navigation[].accessory`、`slots[]`），
 * `Layout.tsx` 只按 `ComponentType` 渲染。所以这里能改，核心一行不动。
 *
 * ## 两个插槽为什么要看 `reachable`
 *
 * `stock-preview.footer`（个股预览页脚）与 `watchlist.toolbar`（自选工具栏）原本无条件
 * 渲染一个「打开研报」链接 —— 服务没起来时点进去是一页坏界面，用户以为是自己的问题。
 *
 * 现在按 `execute` 的**三态**处理：
 *
 * - `reachable === true` → 正常给链接，`/dsa?section=reports&code=`；
 * - `reachable === false` → 降级成**不可点的灰字**「决策服务未连接」，不进主路径；
 * - 尚未拿到结果（loading / 请求失败） → 同灰字「检测中」，**不猜**。
 *
 * **`enabled` 一律不参与判定**：它约等于 `bool(DSA_BASE_URL)`，默认 `.env` 下恒真。
 *
 * ## 关键：不能阻塞主路径
 *
 * 这两个插槽挂在个股预览和自选上 —— 是本仓最热的两个页面。插槽自己 status 挂了、
 * 抛异常了、或者压根没登录，都必须**静默降级成一行字**，绝不能把宿主页面打白。
 * 所以所有渲染分支都返回等高的容器，且 `retry: false`（别为一行提示反复重试）。
 *
 * ## 异动详情「让 DSA 分析」—— 本拍不做
 *
 * `design_pending`。异动页（`AbnormalMoves`）没有挂载点，`FrontendSlotContextMap`
 * 也只有三个 slot 名。硬塞就等于在核心页 import DSA —— 那条线不许越。
 * 见 `docs/dsa-integration.md`「异动入口」。
 */

import { Sparkles } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import type { FrontendExtension, FrontendSlotRegistration } from '@/extensions/types'
import { QK } from '@/lib/queryKeys'
import { DecisionWorkspace } from './DecisionWorkspace'
import { DsaNavReachableAccessory } from './DsaNavReachableAccessory'
import { fetchDsaStatus } from './client'

/**
 * 插槽用的可达性三态钩子。
 *
 * 复用 `QK.dsaStatus` 与 30s 轮询，和导航状态点、页内徽标命中同一份缓存，
 * 不额外打 `/api/dsa/status`。
 */
function useSlotReachability(): boolean | null {
  const status = useQuery({
    queryKey: QK.dsaStatus,
    queryFn: fetchDsaStatus,
    refetchInterval: 30_000,
    retry: false,
  })
  // 只看 reachable；enabled 恒真，拿它判定会把「已配置」误当成「已连通」
  return typeof status.data?.reachable === 'boolean' ? status.data.reachable : null
}

const DEGRADED_CLS = 'text-muted'

function StockReportLink({ symbol, name }: { symbol: string; name: string | null; view: 'daily' | 'intraday' }) {
  const reachable = useSlotReachability()
  if (reachable !== true) {
    // 降级分支：形状与可达分支一致（同一行、同样内边距），只是右侧换成灰字、不可点。
    // 换形状会让页面在 status 到达前后跳一下 —— 这是最热的两个页面之一，别抖。
    return (
      <div
        data-dsa-slot-degraded={reachable === false ? 'offline' : 'unknown'}
        className="flex items-center justify-between gap-3 border-t border-border px-4 py-2 text-xs"
      >
        <span className="truncate text-muted">多市场决策研报</span>
        <span className={`shrink-0 ${DEGRADED_CLS}`}>
          {reachable === false ? '决策服务未连接' : '检测中'}
        </span>
      </div>
    )
  }
  const params = new URLSearchParams({ section: 'reports', code: symbol })
  return (
    <div
      data-dsa-slot="stock-preview.footer"
      data-dsa-slot-reachable="true"
      className="flex items-center justify-between gap-3 border-t border-border px-4 py-2 text-xs"
    >
      <span className="truncate text-muted">{name || symbol} 的多市场决策研报</span>
      <Link className="shrink-0 text-accent hover:underline" to={`/dsa?${params.toString()}`}>
        打开研报
      </Link>
    </div>
  )
}

function WatchlistDecision({ symbols }: { symbols: string[]; viewMode: 'table' | 'card'; selectedGroup: string; refresh: () => void }) {
  const navigate = useNavigate()
  const reachable = useSlotReachability()
  const code = symbols[0]

  // 不可达：仍是 <button disabled>，尺寸与可达态一致。
  // 不用 <a> 或可点按钮 —— 别让人点进坏页面再退回来（那比没有入口更烦）。
  if (reachable !== true) {
    return (
      <button
        type="button"
        disabled
        data-dsa-slot-degraded={reachable === false ? 'offline' : 'unknown'}
        title={reachable === false ? '决策服务未连接，暂不能打开研报' : '正在检测决策服务'}
        className="inline-flex h-8 cursor-not-allowed items-center rounded-btn bg-elevated px-2.5 text-xs text-muted opacity-60"
      >
        决策研报 · {reachable === false ? '未连接' : '检测中'}
      </button>
    )
  }

  return (
    <button
      type="button"
      data-dsa-slot="watchlist.toolbar"
      data-dsa-slot-reachable="true"
      onClick={() => navigate(code ? `/dsa?section=reports&code=${encodeURIComponent(code)}` : '/dsa')}
      className="inline-flex h-8 items-center rounded-btn bg-elevated px-2.5 text-xs text-secondary hover:text-foreground"
    >
      决策研报{symbols.length > 0 ? ` (${symbols.length})` : ''}
    </button>
  )
}

const extension: FrontendExtension = {
  id: 'dsa.workspace',
  apiVersion: 1,
  routes: [
    { id: 'dsa-workspace', path: '/dsa', component: DecisionWorkspace },
  ],
  navigation: [
    {
      id: 'dsa-workspace',
      routeId: 'dsa-workspace',
      label: '决策',
      icon: Sparkles,
      order: 35,
      // 静态标记保持不变: 它只表示「这是 AI 决策区」, 不表示服务可用
      badge: 'AI',
      // 服务可达性走通用 accessory 动态渲染, 以 /api/dsa/status.reachable 为准。
      // 禁止往上塞 enabled 推导出的「就绪」结论 —— enabled 默认近乎恒真。
      accessory: DsaNavReachableAccessory,
    },
  ],
  slots: [
    { name: 'stock-preview.footer', id: 'dsa-stock-report', order: 20, component: StockReportLink },
    { name: 'watchlist.toolbar', id: 'dsa-watchlist-report', order: 20, component: WatchlistDecision },
  ] as unknown as FrontendSlotRegistration[],
}

export default extension
