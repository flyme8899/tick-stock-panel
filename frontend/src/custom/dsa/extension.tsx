import { Sparkles } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import type { FrontendExtension, FrontendSlotRegistration } from '@/extensions/types'
import { DecisionWorkspace } from './DecisionWorkspace'
import { DsaNavReachableAccessory } from './DsaNavReachableAccessory'

function StockReportLink({ symbol, name }: { symbol: string; name: string | null; view: 'daily' | 'intraday' }) {
  const params = new URLSearchParams({ section: 'reports', code: symbol })
  return (
    <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-2 text-xs">
      <span className="truncate text-muted">{name || symbol} 的多市场决策研报</span>
      <Link className="shrink-0 text-accent hover:underline" to={`/dsa?${params.toString()}`}>
        打开研报
      </Link>
    </div>
  )
}

function WatchlistDecision({ symbols }: { symbols: string[]; viewMode: 'table' | 'card'; selectedGroup: string; refresh: () => void }) {
  const navigate = useNavigate()
  const code = symbols[0]
  return (
    <button
      type="button"
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
