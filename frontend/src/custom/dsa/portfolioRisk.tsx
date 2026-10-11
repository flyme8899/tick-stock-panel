import type { ReactNode } from 'react'
import { EmptyState } from '@/components/EmptyState'
import { PieChart } from 'lucide-react'
import { cn } from '@/lib/cn'
import { isRecord } from './client'

function num(value: unknown) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  return null
}

function money(value: unknown) {
  const amount = num(value)
  if (amount == null) return '—'
  return amount.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function pctPoints(value: unknown) {
  const amount = num(value)
  if (amount == null) return '—'
  return `${amount.toFixed(2)}%`
}

function records(value: unknown) {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

function block(value: unknown) {
  return isRecord(value) ? value : {}
}

export interface PortfolioRiskViewModel {
  currency: string
  asOf: string
  hasPositions: boolean
  thresholds: { label: string; value: string }[]
  concentration: {
    totalMarketValue: string
    topWeight: string
    alert: boolean
    positions: { symbol: string; marketValue: string; weight: string; alert: boolean }[]
  }
  sectors: {
    topWeight: string
    alert: boolean
    rows: { sector: string; marketValue: string; weight: string; count: string; alert: boolean }[]
  }
  drawdown: {
    current: string
    max: string
    points: string
    alert: boolean
    fxStale: boolean
  }
  stopLoss: {
    near: string
    triggered: string
    alert: boolean
    items: { symbol: string; cost: string; price: string; loss: string; triggered: boolean }[]
  }
  signalRisk: { symbol: string; action: string }[]
}

export function readPortfolioRisk(data: unknown): PortfolioRiskViewModel | null {
  if (!isRecord(data) || (!isRecord(data.concentration) && !isRecord(data.thresholds))) return null
  const concentration = block(data.concentration)
  const sectors = block(data.sector_concentration)
  const drawdown = block(data.drawdown)
  const stopLoss = block(data.stop_loss)
  const thresholds = block(data.thresholds)
  const positions = records(concentration.top_positions)
  const totalMarketValue = num(concentration.total_market_value) ?? 0
  const nearRatio = num(thresholds.stop_loss_near_ratio)
  const signalBlock = block(data.decision_signal_risk)
  return {
    currency: typeof data.currency === 'string' && data.currency ? data.currency : 'CNY',
    asOf: typeof data.as_of === 'string' ? data.as_of : '—',
    hasPositions: positions.length > 0 || totalMarketValue > 0,
    thresholds: [
      { label: '集中度警戒', value: pctPoints(thresholds.concentration_alert_pct) },
      { label: '回撤警戒', value: pctPoints(thresholds.drawdown_alert_pct) },
      { label: '止损警戒', value: pctPoints(thresholds.stop_loss_alert_pct) },
      { label: '接近止损', value: nearRatio == null ? '—' : nearRatio <= 1 ? `${(nearRatio * 100).toFixed(0)}%` : pctPoints(nearRatio) },
      { label: '回撤窗口', value: num(thresholds.lookback_days) == null ? '—' : `${num(thresholds.lookback_days)} 天` },
    ],
    concentration: {
      totalMarketValue: money(concentration.total_market_value),
      topWeight: pctPoints(concentration.top_weight_pct),
      alert: concentration.alert === true,
      positions: positions.map(row => ({
        symbol: String(row.symbol ?? '—'),
        marketValue: money(row.market_value_base),
        weight: pctPoints(row.weight_pct),
        alert: row.is_alert === true,
      })),
    },
    sectors: {
      topWeight: pctPoints(sectors.top_weight_pct),
      alert: sectors.alert === true,
      rows: records(sectors.top_sectors).map(row => ({
        sector: String(row.sector ?? '—'),
        marketValue: money(row.market_value_base),
        weight: pctPoints(row.weight_pct),
        count: num(row.symbol_count) == null ? '—' : String(num(row.symbol_count)),
        alert: row.is_alert === true,
      })),
    },
    drawdown: {
      current: pctPoints(drawdown.current_drawdown_pct),
      max: pctPoints(drawdown.max_drawdown_pct),
      points: num(drawdown.series_points) == null ? '—' : String(num(drawdown.series_points)),
      alert: drawdown.alert === true,
      fxStale: drawdown.fx_stale === true,
    },
    stopLoss: {
      near: num(stopLoss.near_count) == null ? '—' : String(num(stopLoss.near_count)),
      triggered: num(stopLoss.triggered_count) == null ? '—' : String(num(stopLoss.triggered_count)),
      alert: stopLoss.near_alert === true,
      items: records(stopLoss.items).map(row => ({
        symbol: String(row.symbol ?? '—'),
        cost: money(row.avg_cost),
        price: money(row.last_price),
        loss: pctPoints(row.loss_pct),
        triggered: row.is_triggered === true,
      })),
    },
    signalRisk: records(signalBlock.items).map(row => {
      const signal = block(row.signal)
      return {
        symbol: String(row.symbol ?? '—'),
        action: String(signal.action_label || signal.action || '—'),
      }
    }),
  }
}

function RiskBadge({ alert, on, off }: { alert: boolean; on: string; off: string }) {
  return (
    <span className={cn('shrink-0 rounded-btn px-2 py-0.5 text-[11px]', alert ? 'bg-danger/15 text-danger' : 'bg-elevated text-muted')}>
      {alert ? on : off}
    </span>
  )
}

function Card({ title, extra, children }: { title: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-base">
      <header className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
        <h3 className="text-xs font-medium text-foreground">{title}</h3>
        {extra}
      </header>
      <div className="px-3 py-2">{children}</div>
    </section>
  )
}

export function PortfolioRiskView({ data }: { data: unknown }) {
  const report = readPortfolioRisk(data)
  if (!report) return null
  if (!report.hasPositions) {
    return <EmptyState icon={PieChart} title="DSA 账本暂无持仓" hint="录入持仓后会显示风险。" />
  }
  return (
    <div className="space-y-3">
      <p className="text-[11px] text-muted">截至 {report.asOf} · {report.currency}</p>
      <Card title="阈值">
        <dl data-risk-thresholds className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-5">
          {report.thresholds.map(item => (
            <div key={item.label}>
              <dt className="text-[11px] text-muted">{item.label}</dt>
              <dd className="num text-sm text-foreground">{item.value}</dd>
            </div>
          ))}
        </dl>
      </Card>
      <Card title="持仓集中度" extra={<RiskBadge alert={report.concentration.alert} on="超阈值" off="未触发" />}>
        <dl data-risk-concentration className="mb-2 grid grid-cols-2 gap-3">
          <div>
            <dt className="text-[11px] text-muted">总市值</dt>
            <dd className="num text-sm text-foreground">{report.concentration.totalMarketValue}</dd>
          </div>
          <div>
            <dt className="text-[11px] text-muted">最大权重</dt>
            <dd className={cn('num text-sm', report.concentration.alert ? 'text-danger' : 'text-foreground')}>{report.concentration.topWeight}</dd>
          </div>
        </dl>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[28rem] text-left text-xs">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-3 font-normal">标的</th>
                <th className="py-1 pr-3 font-normal">市值</th>
                <th className="py-1 pr-3 font-normal">权重</th>
                <th className="py-1 font-normal">预警</th>
              </tr>
            </thead>
            <tbody>
              {report.concentration.positions.map(row => (
                <tr key={row.symbol} className="border-t border-border">
                  <td className="whitespace-nowrap py-1.5 pr-3 font-mono text-foreground">{row.symbol}</td>
                  <td className="num whitespace-nowrap py-1.5 pr-3">{row.marketValue}</td>
                  <td className={cn('num whitespace-nowrap py-1.5 pr-3', row.alert && 'text-danger')}>{row.weight}</td>
                  <td className="whitespace-nowrap py-1.5">{row.alert ? '超阈值' : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="行业集中度" extra={<RiskBadge alert={report.sectors.alert} on="超阈值" off="未触发" />}>
        <p data-risk-sectors className="mb-2 text-[11px] text-muted">最大行业权重 {report.sectors.topWeight}</p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[32rem] text-left text-xs">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-3 font-normal">行业</th>
                <th className="py-1 pr-3 font-normal">市值</th>
                <th className="py-1 pr-3 font-normal">权重</th>
                <th className="py-1 pr-3 font-normal">只数</th>
                <th className="py-1 font-normal">预警</th>
              </tr>
            </thead>
            <tbody>
              {report.sectors.rows.map(row => (
                <tr key={row.sector} className="border-t border-border">
                  <td className="whitespace-nowrap py-1.5 pr-3 text-foreground">{row.sector}</td>
                  <td className="num whitespace-nowrap py-1.5 pr-3">{row.marketValue}</td>
                  <td className={cn('num whitespace-nowrap py-1.5 pr-3', row.alert && 'text-danger')}>{row.weight}</td>
                  <td className="num whitespace-nowrap py-1.5 pr-3">{row.count}</td>
                  <td className="whitespace-nowrap py-1.5">{row.alert ? '超阈值' : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="回撤" extra={<RiskBadge alert={report.drawdown.alert} on="超阈值" off="未触发" />}>
        <dl data-risk-drawdown className="grid grid-cols-3 gap-3">
          <div>
            <dt className="text-[11px] text-muted">当前回撤</dt>
            <dd className="num text-sm text-foreground">{report.drawdown.current}</dd>
          </div>
          <div>
            <dt className="text-[11px] text-muted">最大回撤</dt>
            <dd className={cn('num text-sm', report.drawdown.alert ? 'text-danger' : 'text-foreground')}>{report.drawdown.max}</dd>
          </div>
          <div>
            <dt className="text-[11px] text-muted">样本天数</dt>
            <dd className="num text-sm text-foreground">{report.drawdown.points}</dd>
          </div>
        </dl>
        {report.drawdown.fxStale && <p className="mt-2 text-[11px] text-warning">汇率不是最新，回撤按当时可得的汇率折算。</p>}
      </Card>
      <Card title="止损" extra={<RiskBadge alert={report.stopLoss.alert} on="接近或已触发" off="未触发" />}>
        <p data-risk-stop className="mb-2 text-[11px] text-muted">接近 {report.stopLoss.near} 只 · 已触发 {report.stopLoss.triggered} 只</p>
        {report.stopLoss.items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[32rem] text-left text-xs">
              <thead className="text-muted">
                <tr>
                  <th className="py-1 pr-3 font-normal">标的</th>
                  <th className="py-1 pr-3 font-normal">成本</th>
                  <th className="py-1 pr-3 font-normal">现价</th>
                  <th className="py-1 pr-3 font-normal">浮亏</th>
                  <th className="py-1 font-normal">状态</th>
                </tr>
              </thead>
              <tbody>
                {report.stopLoss.items.map(row => (
                  <tr key={row.symbol} className="border-t border-border">
                    <td className="whitespace-nowrap py-1.5 pr-3 font-mono text-foreground">{row.symbol}</td>
                    <td className="num whitespace-nowrap py-1.5 pr-3">{row.cost}</td>
                    <td className="num whitespace-nowrap py-1.5 pr-3">{row.price}</td>
                    <td className="num whitespace-nowrap py-1.5 pr-3 text-bear">{row.loss}</td>
                    <td className="whitespace-nowrap py-1.5">{row.triggered ? '已触发' : '接近'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {report.signalRisk.length > 0 && (
        <Card title="持仓上的防守信号">
          <ul className="divide-y divide-border text-xs">
            {report.signalRisk.map(row => (
              <li key={`${row.symbol}-${row.action}`} className="flex justify-between gap-3 py-1.5">
                <span className="font-mono text-foreground">{row.symbol}</span>
                <span className="text-danger">{row.action}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}
