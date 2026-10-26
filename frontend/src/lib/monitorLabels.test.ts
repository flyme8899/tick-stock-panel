import { expect, it } from 'vitest'
import {
  abnormalWindowLabel,
  hitDirectionLabel,
  hitFamilyLabel,
  monitorBadgeText,
  monitorDisplayText,
  monitorTagLabel,
} from './monitorLabels'

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

it('maps buy-sell hit families to chips and refuses to label composite', () => {
  expect(hitFamilyLabel('minute_event')).toBe('事件')
  expect(hitFamilyLabel('daily_fundamental')).toBe('基本面')
  // composite 首批不对外: 没有入口, 也不给兜底文案
  expect(hitFamilyLabel('composite')).toBeNull()
  expect(hitFamilyLabel('')).toBeNull()
  expect(hitFamilyLabel(null)).toBeNull()
  expect(hitFamilyLabel(undefined)).toBeNull()
})

it('maps direction_label to research wording, never to trading verbs', () => {
  expect(hitDirectionLabel('research_buy')).toBe('研究·偏多')
  expect(hitDirectionLabel('research_sell')).toBe('研究·偏空')
  expect(hitDirectionLabel('research_watch')).toBe('研究·观察')
  expect(hitDirectionLabel('buy')).toBeNull()
  expect(hitDirectionLabel('')).toBeNull()

  // 「买入 / 卖出」是可执行动作, 命中层不该出现
  for (const key of ['research_buy', 'research_sell', 'research_watch']) {
    const label = hitDirectionLabel(key)!
    expect(label).not.toContain('买入')
    expect(label).not.toContain('卖出')
    expect(label).toContain('研究')
  }
})
