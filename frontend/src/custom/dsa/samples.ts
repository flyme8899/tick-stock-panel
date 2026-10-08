/** 界面样例。服务未连接时用于展示版式，不代表真实行情或建议。 */

export const SAMPLE_HISTORY = [
  {
    id: 1,
    stock_code: '600519',
    stock_name: '贵州茅台',
    operation_advice: '观望',
    sentiment_score: 72,
    trend_prediction: '看多',
    analysis_summary: '业绩支撑仍在，短线获利盘需要消化，等待回踩后再评估。',
    region: 'cn',
    created_at: '2026-10-08 18:02',
  },
  {
    id: 2,
    stock_code: 'hk00700',
    stock_name: '腾讯控股',
    operation_advice: '买入',
    sentiment_score: 81,
    trend_prediction: '看多',
    analysis_summary: '广告与游戏业务预期改善，回撤后重新站上中期均线。',
    region: 'hk',
    created_at: '2026-10-08 18:04',
  },
  {
    id: 3,
    stock_code: 'AAPL',
    stock_name: '苹果',
    operation_advice: '卖出',
    sentiment_score: 38,
    trend_prediction: '看空',
    analysis_summary: '新品周期交易拥挤，估值对下修指引敏感。',
    region: 'us',
    created_at: '2026-10-08 18:06',
  },
  {
    id: 4,
    stock_code: '2330.TW',
    stock_name: '台积电',
    operation_advice: '观望',
    sentiment_score: 66,
    trend_prediction: '震荡',
    analysis_summary: '先进制程订单能见度高，但股价已反映大部分扩产预期。',
    region: 'tw',
    created_at: '2026-10-08 18:08',
  },
]

export const SAMPLE_REPORT = `# 贵州茅台 (600519) 决策报告

> 样例文本，用于核对版式。服务连接后会替换成真实报告。

## 核心结论
评分 **72**，建议 **观望**，趋势看多。当前位置不追高，回踩关键均线再评估。

## 风险警报
- 短期成交额萎缩，上攻动能不足
- 批价预期若下修，估值会先压缩

## 催化因素
- 旺季动销若超预期，渠道库存有望回落
- 分红政策仍是中长期资金的主要锚

## 检查清单
1. 确认公告日不晚于行情日
2. 不把样例分数当成下单依据
`

export const SAMPLE_NEWS = [
  { title: '白酒批价企稳，渠道反馈节后动销分化', source: '样例情报', published_at: '2026-10-08 09:20' },
  { title: '港股互联网广告价格回升，游戏流水好于上季', source: '样例情报', published_at: '2026-10-08 10:05' },
  { title: '费城半导体指数创新高，设备与代工同步走强', source: '样例情报', published_at: '2026-10-08 11:40' },
]

export const SAMPLE_SCREEN = [
  { code: '300750', name: '宁德时代', market: 'cn', score: 86, reason: '量价齐升且趋势未破' },
  { code: 'hk09988', name: '阿里巴巴', market: 'hk', score: 74, reason: '事件驱动后的缩量整理' },
  { code: 'NVDA', name: '英伟达', market: 'us', score: 69, reason: '高位波动加大，只保留观察' },
]

export const SAMPLE_RISK = [
  { name: '行业集中', level: '偏高', detail: '样例组合里消费权重超过 40%' },
  { name: '单一标的', level: '中性', detail: '最大持仓约占净值 18%' },
  { name: '汇率', level: '偏低', detail: '港股与美股敞口有部分对冲' },
]

export const SAMPLE_ALERTS = [
  { name: '茅台跌破参考位', symbol: '600519', status: '启用', last: '尚未触发' },
  { name: '苹果评分降至 40', symbol: 'AAPL', status: '启用', last: '样例触发 · 10-08 18:06' },
]
