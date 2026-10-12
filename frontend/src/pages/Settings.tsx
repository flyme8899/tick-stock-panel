/**
 * 统一设置页面 — Tab 切换外壳。
 *
 * 通过 URL query param ?tab=xxx 同步 Tab 状态。
 */
import { Suspense, lazy, useState, type ComponentType } from 'react'
import { useSearchParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { BarChart3, Database, KeyRound, Lock, Radio, SlidersHorizontal, Sparkles, Settings2, PanelLeftClose, PanelLeftOpen, Clock3 } from 'lucide-react'
// 面板按 tab 按需加载, 同步打包会让设置页 chunk 膨胀
const SettingsAIPanel = lazy(() => import('./settings/AI').then(m => ({ default: m.SettingsAIPanel })))
const SettingsApiTokensPanel = lazy(() => import('./settings/ApiTokens').then(m => ({ default: m.SettingsApiTokensPanel })))
const SettingsMonitoringPanel = lazy(() => import('./settings/Monitoring').then(m => ({ default: m.SettingsMonitoringPanel })))
const SettingsExtPagesPanel = lazy(() => import('./settings/ExtPages').then(m => ({ default: m.SettingsExtPagesPanel })))
const SettingsMenuSettingsPanel = lazy(() => import('./settings/MenuSettings').then(m => ({ default: m.SettingsMenuSettingsPanel })))
const SettingsTimeoutPanel = lazy(() => import('./settings/Timeout').then(m => ({ default: m.SettingsTimeoutPanel })))
const SettingsSystemPanel = lazy(() => import('./settings/System').then(m => ({ default: m.SettingsSystemPanel })))
const SettingsPasswordPanel = lazy(() => import('./settings/Password').then(m => ({ default: m.SettingsPasswordPanel })))
const SettingsDataSourcesPanel = lazy(() => import('./settings/DataSources').then(m => ({ default: m.SettingsDataSourcesPanel })))
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

// ===== Tab 定义 =====

type TabDef = {
  key: string
  label: string
  icon: ComponentType<{ className?: string }>
  panel: ComponentType<{ highlight?: string }>
  badge?: string
}

const TABS: readonly TabDef[] = [
  { key: 'data-sources', label: '数据源',     icon: Database,  panel: SettingsDataSourcesPanel },
  { key: 'ai',         label: 'AI 设置',    icon: Sparkles,  panel: SettingsAIPanel },
  { key: 'monitoring', label: '实时监控',   icon: Radio,     panel: SettingsMonitoringPanel },
  { key: 'ext-pages',  label: '扩展页面',   icon: BarChart3, panel: SettingsExtPagesPanel },
  { key: 'timeout',    label: '网络设置',   icon: Clock3,    panel: SettingsTimeoutPanel },
  { key: 'menus',      label: '菜单设置',   icon: SlidersHorizontal, panel: SettingsMenuSettingsPanel },
  { key: 'api-tokens', label: '开放接口',   icon: KeyRound, panel: SettingsApiTokensPanel },
  { key: 'system',     label: '系统设置',   icon: Settings2, panel: SettingsSystemPanel },
  { key: 'password',   label: '修改密码',   icon: Lock,      panel: SettingsPasswordPanel },
]

type TabKey = (typeof TABS)[number]['key']

export function Settings() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab') as TabKey | null
  const activeTab = TABS.find((t) => t.key === tabParam) ?? TABS[0]
  const highlight = searchParams.get('highlight') ?? ''

  // 设置菜单收起状态 — 持久化到 localStorage
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('tf-settings-nav-collapsed') === '1' } catch { return false }
  })
  const toggleCollapsed = () => {
    setCollapsed(prev => {
      const next = !prev
      try { localStorage.setItem('tf-settings-nav-collapsed', next ? '1' : '0') } catch {}
      return next
    })
  }

  return (
    <>
      <PageHeader
        title="设置"
        helpId="settings"
        subtitle="管理账户、数据刷新策略和高级功能配置。"
      />

      <div className="px-4 py-4 md:px-8 md:py-6">
        <div className="flex flex-col gap-4 md:flex-row md:items-stretch md:gap-6">
          {/* 窄屏横滑页签；md 及以上保持左侧竖栏 */}
          <nav className={cn(
            'w-full min-w-0 shrink-0 md:transition-all md:duration-200 md:ease-smooth',
            collapsed ? 'md:w-10' : 'md:w-36',
          )}>
            <div className="sticky top-6 flex gap-1 overflow-x-auto pb-1 md:min-h-[60vh] md:flex-col md:justify-center md:gap-0.5 md:overflow-visible md:pb-0">
              <button
                onClick={toggleCollapsed}
                className={cn(
                  'mb-1 hidden items-center gap-2 rounded-btn text-muted transition-colors duration-150 ease-smooth hover:bg-elevated/60 hover:text-foreground md:flex',
                  collapsed ? 'justify-center px-0 py-2' : 'px-3 py-2 text-xs',
                )}
                title={collapsed ? '展开菜单' : '收起菜单'}
              >
                {collapsed
                  ? <PanelLeftOpen className="h-3.5 w-3.5 shrink-0" />
                  : <PanelLeftClose className="h-3.5 w-3.5 shrink-0" />
                }
                {!collapsed && <span>收起菜单</span>}
              </button>

              {TABS.map(({ key, label, icon: Icon, badge }) => (
                <button
                  key={key}
                  onClick={() => setSearchParams({ tab: key }, { replace: true })}
                  title={collapsed ? label : undefined}
                  className={cn(
                    'relative flex shrink-0 items-center gap-2 whitespace-nowrap rounded-btn px-3 py-2 text-sm transition-colors duration-150 ease-smooth',
                    collapsed && 'md:justify-center md:px-0',
                    activeTab.key === key
                      ? 'bg-accent/10 text-accent font-medium'
                      : 'text-secondary hover:text-foreground hover:bg-elevated/60',
                  )}
                >
                  <Icon className="h-3.5 w-3.5 shrink-0" />
                  <span className={cn(collapsed && 'md:hidden')}>{label}</span>
                  {badge && (
                    <span className={cn(
                      'ml-auto inline-flex shrink-0 items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400',
                      collapsed && 'md:hidden',
                    )}>
                      {badge}
                    </span>
                  )}
                </button>
              ))}
            </div>
          </nav>

          {/* ===== Tab 内容 ===== */}
          <motion.div
            key={activeTab.key}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="min-w-0 w-full flex-1"
          >
            <Suspense fallback={<div className="py-12 text-center text-xs text-muted">加载中…</div>}>
              {activeTab.key === 'monitoring'
                ? <SettingsMonitoringPanel highlight={highlight} />
                : <activeTab.panel highlight={highlight} />}
            </Suspense>
          </motion.div>
        </div>
      </div>
    </>
  )
}
