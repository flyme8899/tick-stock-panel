/** 界面样例。服务未连接时用于展示版式，不代表真实行情或建议。 */

/** 样例观察池，对应定时任务要跟踪的代码。 */
export const SAMPLE_WATCH_POOL = ['603626', '300750', '600519']

export const SAMPLE_HISTORY = [
  {
    id: 1,
    stock_code: '600519',
    stock_name: '贵州茅台',
    operation_advice: '观望',
    current_price: 1488,
    change_pct: -0.42,
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
  {
    id: 5,
    stock_code: '603626.SH',
    stock_name: '科森科技',
    current_price: 18.36,
    change_pct: 2.15,
    report_type: 'detailed',
    operation_advice: '观望',
    sentiment_score: 58,
    trend_prediction: '看多',
    analysis_summary: '均线呈标准多头排列，趋势仍在，短线不追高。',
    region: 'cn',
    created_at: '2026-10-10 18:12',
  },
  {
    id: 6,
    stock_code: '300750',
    stock_name: '宁德时代',
    current_price: 215.4,
    change_pct: -1.08,
    report_type: 'detailed',
    operation_advice: '观望',
    sentiment_score: 45,
    trend_prediction: '震荡',
    analysis_summary: '收涨后放量反弹，但均线仍未走平。',
    region: 'cn',
    created_at: '2026-10-10 18:16',
  },
  {
    id: 7,
    stock_code: 'MARKET',
    stock_name: '大盘复盘',
    report_type: 'market_review',
    operation_advice: '查看复盘',
    sentiment_score: 50,
    trend_prediction: '大盘复盘',
    analysis_summary: '2026-10-10 大盘复盘 早盘',
    region: 'cn',
    created_at: '2026-10-10 15:10',
  },
  {
    id: 8,
    stock_code: 'MARKET',
    stock_name: '大盘复盘',
    report_type: 'market_review',
    operation_advice: '查看复盘',
    sentiment_score: 50,
    trend_prediction: '大盘复盘',
    analysis_summary: '2026-10-10 大盘复盘 收盘',
    region: 'cn',
    created_at: '2026-10-10 18:40',
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

export const SAMPLE_RISK = {
  as_of: '2026-10-10',
  currency: 'CNY',
  cost_method: 'fifo',
  thresholds: {
    concentration_alert_pct: 35,
    drawdown_alert_pct: 15,
    stop_loss_alert_pct: 10,
    stop_loss_near_ratio: 0.8,
    lookback_days: 180,
  },
  concentration: {
    total_market_value: 1280000,
    top_weight_pct: 42.5,
    alert: true,
    top_positions: [
      { symbol: '600519', market_value_base: 544000, weight_pct: 42.5, is_alert: true },
      { symbol: '000858', market_value_base: 320000, weight_pct: 25, is_alert: false },
    ],
  },
  sector_concentration: {
    total_market_value: 1280000,
    top_weight_pct: 67.5,
    alert: true,
    top_sectors: [
      { sector: '白酒', market_value_base: 864000, weight_pct: 67.5, symbol_count: 2, is_alert: true },
    ],
  },
  drawdown: {
    series_points: 40,
    max_drawdown_pct: 8.2,
    current_drawdown_pct: 3.1,
    alert: false,
    fx_stale: false,
  },
  stop_loss: {
    near_alert: true,
    triggered_count: 1,
    near_count: 1,
    items: [
      { symbol: '000858', avg_cost: 120, last_price: 105, loss_pct: 12.5, is_triggered: true },
    ],
  },
}

export const SAMPLE_ALERTS = [
  { name: '茅台跌破参考位', symbol: '600519', status: '启用', last: '尚未触发' },
  { name: '苹果评分降至 40', symbol: 'AAPL', status: '启用', last: '样例触发 · 10-08 18:06' },
]
