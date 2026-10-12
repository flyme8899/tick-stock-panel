import { expect, it } from 'vitest'
import { abnormalWindowLabel, monitorBadgeText, monitorDisplayText, monitorTagLabel } from './monitorLabels'

it('maps monitor signal types to Chinese and never returns the raw key', () => {
  expect(monitorTagLabel('volume_delta')).toBe('放量')
  expect(monitorTagLabel('abnormal')).toBe('异动')
  expect(monitorTagLabel('abnormal_up')).toBe('上涨异动')
  expect(monitorTagLabel('date_reminder')).toBe('日期')
  expect(monitorTagLabel('ladder')).toBe('封单')
  expect(monitorTagLabel('paper')).toBe('模拟盘')
  expect(monitorTagLabel('paper_fill')).toBe('模拟盘')
  expect(monitorTagLabel('buy_signal')).toBe('买入')
  expect(monitorTagLabel('sector_momentum_down')).toBe('板块')
  expect(monitorTagLabel('signal_volume_surge')).toBe('放量')
  expect(monitorTagLabel('unknown_signal_key')).toBe('其他')
  expect(monitorTagLabel('')).toBe('其他')
  expect(monitorTagLabel('轮询放量')).toBe('轮询放量')
  expect(monitorTagLabel('csg_custom', { csg_custom: '我的金叉' })).toBe('我的金叉')
})

it('rewrites interval seconds and deviation windows in alert text', () => {
  expect(monitorDisplayText('放量 · 单轮增量 9,500 手 >= 9,000 手 (间隔 3s)')).toBe(
    '放量 · 单轮增量 9,500 手 >= 9,000 手 (间隔 3秒)',
  )
  expect(monitorDisplayText('间隔 6S')).toContain('间隔 6秒')
  expect(monitorDisplayText('平安银行 3d偏离值 +1.60%/阈值20%')).toBe(
    '平安银行 3日偏离值 +1.60%/阈值20%',
  )
  expect(monitorDisplayText('接近度≥70% (3D)')).toBe('接近度≥70% (3日)')
  expect(monitorBadgeText('volume_delta', 'volume_delta')).toBe('放量')
  expect(monitorBadgeText('', 'abnormal')).toBe('异动')
  expect(monitorBadgeText('接近度≥70% (3D)', 'abnormal')).toBe('接近度≥70% (3日)')
  expect(monitorBadgeText('MACD金叉', 'strategy')).toBe('MACD金叉')
  expect(abnormalWindowLabel('3d')).toBe('3日窗口')
  expect(abnormalWindowLabel('10D')).toBe('10日窗口')
  expect(abnormalWindowLabel('any')).toBe('全部窗口')
  expect(abnormalWindowLabel('nope')).toBe('其他窗口')
})
