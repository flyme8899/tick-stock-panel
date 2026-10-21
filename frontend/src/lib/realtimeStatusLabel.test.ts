import { describe, expect, it } from 'vitest'
import { realtimeQuoteStatusLabel } from './realtimeStatusLabel'

const base = {
  pending: false,
  paused: false,
  enabled: true,
  running: false,
  isTrading: false,
}

describe('侧栏实时行情状态', () => {
  it('集合竞价：轮询窗口且 market_phase 为 preopen', () => {
    expect(realtimeQuoteStatusLabel({
      ...base,
      isPollingWindow: true,
      marketPhase: 'preopen',
    })).toBe('集合竞价中')
  })

  it('其余状态保持原优先级', () => {
    expect(realtimeQuoteStatusLabel({ ...base, pending: true, isPollingWindow: true, marketPhase: 'preopen' })).toBe('正在更新')
    expect(realtimeQuoteStatusLabel({ ...base, paused: true, isPollingWindow: true, marketPhase: 'preopen' })).toBe('同步期间暂停')
    expect(realtimeQuoteStatusLabel({ ...base, running: true, isTrading: true, isPollingWindow: true, marketPhase: 'preopen' })).toBe('运行中')
    expect(realtimeQuoteStatusLabel({ ...base, enabled: false, isPollingWindow: true, marketPhase: 'preopen' })).toBe('已关闭')
    expect(realtimeQuoteStatusLabel({ ...base, isTrading: true })).toBe('正在连接')
    expect(realtimeQuoteStatusLabel({ ...base, isPollingWindow: true, marketPhase: 'trading' })).toBe('等待交易时段')
    expect(realtimeQuoteStatusLabel(base)).toBe('等待交易时段')
  })
})
