import { describe, expect, it } from 'vitest'
import { HELP_TOPICS, helpTopic } from './guide'

const REQUIRED = [
  '看板',
  '热门事件',
  '资金流向',
  '板块分析',
  '涨停梯队',
  '选股',
  '回测 / 因子挖掘',
  '自选 / 观察池',
  'DSA 决策',
  '钉钉推送',
  '设置 / 账号',
]

describe('help topics', () => {
  it('covers each requested feature once', () => {
    expect(HELP_TOPICS.map(topic => topic.title)).toEqual(REQUIRED)
    expect(new Set(HELP_TOPICS.map(topic => topic.id)).size).toBe(HELP_TOPICS.length)
  })

  it('keeps code-backed thresholds in the prose', () => {
    const text = HELP_TOPICS.map(topic => `${topic.summary}\n${topic.markdown}`).join('\n')
    const facts = [
      '2.5',
      '财联社',
      '不低于 2',
      '9:25',
      '5、15、30',
      '重大 130',
      '12 小时',
      '600 秒',
      '前 8',
      '30 只',
      'FUND_FLOW_ENABLED',
      '3 分钟',
      '6 分钟',
      '盘中资金净额(同花顺)',
      'FUND_FLOW_STOCK_INTRADAY',
      '16:30',
      '并集',
      '交集',
      '百分数',
      '0.8 表示 80%',
      '1 到 30',
      '15:35',
      '默认关闭',
      '周五',
      '30 分钟',
      '5 个自然日',
      '理想买入',
      '18:00',
      'Asia/Shanghai',
      '200',
      '08:45',
      '15:40',
      '每分钟最多 20',
      '1.5%',
      '0.3%',
      '10:00',
      'PORTFOLIO_RISK_CONCENTRATION_ALERT_PCT',
      '至少 6 位',
      '连续登录失败 5 次',
      '锁定 5 分钟',
      '30 天',
      '可配置',
    ]
    for (const fact of facts) {
      expect(text, fact).toContain(fact)
    }
  })

  it('resolves a topic and ignores unknown ids', () => {
    expect(helpTopic('dingtalk')?.title).toBe('钉钉推送')
    expect(helpTopic('missing')).toBeUndefined()
  })
})
