/** 侧栏「实时行情」状态文案。集合竞价只改 preopen 这一支，其余保持原优先级。 */

export interface RealtimeQuoteStatusInput {
  pending: boolean
  paused: boolean
  enabled: boolean
  running: boolean
  isTrading: boolean
  isPollingWindow?: boolean
  marketPhase?: string | null
}

export function realtimeQuoteStatusLabel(input: RealtimeQuoteStatusInput): string {
  if (input.pending) return '正在更新'
  if (input.paused) return '同步期间暂停'
  if (input.enabled && input.running && input.isTrading) return '运行中'
  if (!input.enabled) return '已关闭'
  if (input.isTrading) return '正在连接'
  if (input.isPollingWindow && input.marketPhase === 'preopen') return '集合竞价中'
  return '等待交易时段'
}
