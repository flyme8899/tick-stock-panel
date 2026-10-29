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
      // 20261029gguide: 帮助页「设置 → 数据来源」已补单股分钟 K 五档周期
      // 1m/5m/15m/30m/60m，这里钉住它，防止再被重写帮助页时无声丢掉。
      //
      // 20261028fcitest: 原来这里钉的是 '5、15、30'（分钟 K 的三档周期）。
      // #170 重写 guide.ts 时把这句删了，测例没跟着改 —— 本条从 2026-10-19 起
      // 一直是红的，只因为此前 CI 从不跑前端全量测例才没人发现。
      //
      // 产品事实写在 capability-labels.tsx 的 `kline.minute.by_symbol`：
      // 「单股 1m/5m/15m/30m/60m K 线」，是**五档**不是三档，措辞也不同，
      // 硬塞回 '5、15、30' 反而是错的 —— 那条过时锚**不要**加回来。
      // 帮助页缺五档周期的缺口已由 20261029gguide 补齐（见上方锚）。
      '1m/5m/15m/30m/60m',
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
