/**
 * 策略结果列表自定义列配置。
 *
 * 内置列与分组与自选页 (watchlist-columns) 保持一致，额外保留策略特有的
 * 「策略」「评分」两列。通用列模型/合并/扩展列参数来自 list-columns。
 */
import { storage } from '@/lib/storage'
import {
  buildExtColumnsParam,
  mergeColumns,
  serializeColumns,
  type ColumnConfig,
  type ColumnGroup,
  type ColumnSource,
  type ExtColumnDisplayConfig,
  type CandleColumnConfig,
} from '@/lib/list-columns'

export type { ColumnConfig, ColumnGroup, ColumnSource, ExtColumnDisplayConfig, CandleColumnConfig }
export { buildExtColumnsParam, mergeColumns, serializeColumns }

export const SCREENER_BUILTIN_COLUMNS: ColumnConfig[] = [
  // 固定列
  { id: 'builtin:symbol', source: { type: 'builtin', key: 'symbol' }, label: '标的', visible: true, pinned: true, align: 'left' },
  // 策略特有列
  { id: 'builtin:strategies', source: { type: 'builtin', key: 'strategies' }, label: '策略', visible: true, align: 'left' },
  { id: 'builtin:score', source: { type: 'builtin', key: 'score' }, label: '评分', visible: true, align: 'right' },
  // 价格
  { id: 'builtin:price', source: { type: 'builtin', key: 'price' }, label: '现价', visible: true, align: 'right' },
  { id: 'builtin:pct', source: { type: 'builtin', key: 'pct' }, label: '涨跌幅', visible: true, align: 'right' },
  { id: 'builtin:change_amount', source: { type: 'builtin', key: 'change_amount' }, label: '涨跌额', visible: false, align: 'right' },
  { id: 'builtin:amplitude', source: { type: 'builtin', key: 'amplitude' }, label: '振幅', visible: false, align: 'right' },
  // 成交
  { id: 'builtin:turnover', source: { type: 'builtin', key: 'turnover' }, label: '换手率', visible: false, align: 'right' },
  { id: 'builtin:amount', source: { type: 'builtin', key: 'amount' }, label: '成交额', visible: true, align: 'right' },
  { id: 'builtin:float_val', source: { type: 'builtin', key: 'float_val' }, label: '流通值', visible: false, align: 'right' },
  { id: 'builtin:vol_ratio', source: { type: 'builtin', key: 'vol_ratio' }, label: '量比', visible: true, align: 'right' },
  { id: 'builtin:annual_vol', source: { type: 'builtin', key: 'annual_vol' }, label: '年化波动', visible: false, align: 'right' },
  // 均线
  { id: 'builtin:ma5', source: { type: 'builtin', key: 'ma5' }, label: 'MA5', visible: false, align: 'right' },
  { id: 'builtin:ma10', source: { type: 'builtin', key: 'ma10' }, label: 'MA10', visible: false, align: 'right' },
  { id: 'builtin:ma20', source: { type: 'builtin', key: 'ma20' }, label: 'MA20', visible: false, align: 'right' },
  { id: 'builtin:ma60', source: { type: 'builtin', key: 'ma60' }, label: 'MA60', visible: false, align: 'right' },
  // 区间
  { id: 'builtin:high_60d', source: { type: 'builtin', key: 'high_60d' }, label: '60日高', visible: false, align: 'right' },
  { id: 'builtin:low_60d', source: { type: 'builtin', key: 'low_60d' }, label: '60日低', visible: false, align: 'right' },
  // 技术指标
  { id: 'builtin:rsi6', source: { type: 'builtin', key: 'rsi6' }, label: 'RSI6', visible: false, align: 'right' },
  { id: 'builtin:rsi14', source: { type: 'builtin', key: 'rsi14' }, label: 'RSI14', visible: true, align: 'right' },
  { id: 'builtin:rsi24', source: { type: 'builtin', key: 'rsi24' }, label: 'RSI24', visible: false, align: 'right' },
  { id: 'builtin:macd_dif', source: { type: 'builtin', key: 'macd_dif' }, label: 'MACD-DIF', visible: false, align: 'right' },
  { id: 'builtin:macd_dea', source: { type: 'builtin', key: 'macd_dea' }, label: 'MACD-DEA', visible: false, align: 'right' },
  { id: 'builtin:macd_hist', source: { type: 'builtin', key: 'macd_hist' }, label: 'MACD柱', visible: false, align: 'right' },
  { id: 'builtin:kdj_k', source: { type: 'builtin', key: 'kdj_k' }, label: 'KDJ-K', visible: false, align: 'right' },
  { id: 'builtin:kdj_d', source: { type: 'builtin', key: 'kdj_d' }, label: 'KDJ-D', visible: false, align: 'right' },
  { id: 'builtin:kdj_j', source: { type: 'builtin', key: 'kdj_j' }, label: 'KDJ-J', visible: false, align: 'right' },
  { id: 'builtin:boll_upper', source: { type: 'builtin', key: 'boll_upper' }, label: '布林上轨', visible: false, align: 'right' },
  { id: 'builtin:boll_lower', source: { type: 'builtin', key: 'boll_lower' }, label: '布林下轨', visible: false, align: 'right' },
  { id: 'builtin:atr14', source: { type: 'builtin', key: 'atr14' }, label: 'ATR14', visible: false, align: 'right' },
  { id: 'builtin:vol_ma5', source: { type: 'builtin', key: 'vol_ma5' }, label: '量MA5', visible: false, align: 'right' },
  { id: 'builtin:vol_ma10', source: { type: 'builtin', key: 'vol_ma10' }, label: '量MA10', visible: false, align: 'right' },
  // 动量
  { id: 'builtin:momentum_5d', source: { type: 'builtin', key: 'momentum_5d' }, label: '5D 动量', visible: false, align: 'right' },
  { id: 'builtin:momentum_10d', source: { type: 'builtin', key: 'momentum_10d' }, label: '10D 动量', visible: false, align: 'right' },
  { id: 'builtin:momentum_20d', source: { type: 'builtin', key: 'momentum_20d' }, label: '20D 动量', visible: false, align: 'right' },
  { id: 'builtin:momentum_30d', source: { type: 'builtin', key: 'momentum_30d' }, label: '30D 动量', visible: false, align: 'right' },
  { id: 'builtin:momentum_60d', source: { type: 'builtin', key: 'momentum_60d' }, label: '60D 动量', visible: true, align: 'right' },
  // 异动偏离 (交易所异动规则口径, 运行时列; 默认隐藏, 在列组「异动」中开启)
  { id: 'builtin:deviate_3d', source: { type: 'builtin', key: 'deviate_3d' }, label: '3D 偏离', visible: false, align: 'right' },
  { id: 'builtin:deviate_10d', source: { type: 'builtin', key: 'deviate_10d' }, label: '10D 偏离', visible: false, align: 'right' },
  { id: 'builtin:deviate_30d', source: { type: 'builtin', key: 'deviate_30d' }, label: '30D 偏离', visible: false, align: 'right' },
  // 连板
  { id: 'builtin:limit_ups', source: { type: 'builtin', key: 'limit_ups' }, label: '连板', visible: true, align: 'center' },
  { id: 'builtin:limit_downs', source: { type: 'builtin', key: 'limit_downs' }, label: '连跌', visible: false, align: 'center' },
  // 选股补充。周/月涨跌和连涨由日 K 现算；北向、股东、指数、解禁、PE 来自收盘后任务。
  { id: 'builtin:northbound_hold_pct', source: { type: 'builtin', key: 'northbound_hold_pct' }, label: '北向持股(季)', visible: true, align: 'right' },
  { id: 'builtin:holder_num', source: { type: 'builtin', key: 'holder_num' }, label: '股东户数', visible: true, align: 'right' },
  { id: 'builtin:holder_change_pct', source: { type: 'builtin', key: 'holder_change_pct' }, label: '股东增减', visible: false, align: 'right' },
  { id: 'builtin:index_membership', source: { type: 'builtin', key: 'index_membership' }, label: '指数成分', visible: true, align: 'left' },
  { id: 'builtin:next_unlock_date', source: { type: 'builtin', key: 'next_unlock_date' }, label: '解禁时间', visible: true, align: 'left' },
  { id: 'builtin:unlock_float_ratio', source: { type: 'builtin', key: 'unlock_float_ratio' }, label: '解禁占流通', visible: false, align: 'right' },
  { id: 'builtin:weekly_change_pct', source: { type: 'builtin', key: 'weekly_change_pct' }, label: '周涨跌', visible: true, align: 'right' },
  { id: 'builtin:monthly_change_pct', source: { type: 'builtin', key: 'monthly_change_pct' }, label: '月涨跌', visible: true, align: 'right' },
  { id: 'builtin:up_streak', source: { type: 'builtin', key: 'up_streak' }, label: '连涨', visible: true, align: 'center' },
  { id: 'builtin:down_streak', source: { type: 'builtin', key: 'down_streak' }, label: '连跌天数', visible: false, align: 'center' },
  { id: 'builtin:macd_hist_w', source: { type: 'builtin', key: 'macd_hist_w' }, label: '周MACD', visible: false, align: 'right' },
  { id: 'builtin:macd_dif_w', source: { type: 'builtin', key: 'macd_dif_w' }, label: '周DIF', visible: false, align: 'right' },
  { id: 'builtin:macd_dea_w', source: { type: 'builtin', key: 'macd_dea_w' }, label: '周DEA', visible: false, align: 'right' },
  { id: 'builtin:kdj_k_w', source: { type: 'builtin', key: 'kdj_k_w' }, label: '周KDJ-K', visible: false, align: 'right' },
  { id: 'builtin:kdj_d_w', source: { type: 'builtin', key: 'kdj_d_w' }, label: '周KDJ-D', visible: false, align: 'right' },
  { id: 'builtin:kdj_j_w', source: { type: 'builtin', key: 'kdj_j_w' }, label: '周KDJ-J', visible: false, align: 'right' },
  { id: 'builtin:macd_hist_m', source: { type: 'builtin', key: 'macd_hist_m' }, label: '月MACD', visible: false, align: 'right' },
  { id: 'builtin:macd_dif_m', source: { type: 'builtin', key: 'macd_dif_m' }, label: '月DIF', visible: false, align: 'right' },
  { id: 'builtin:macd_dea_m', source: { type: 'builtin', key: 'macd_dea_m' }, label: '月DEA', visible: false, align: 'right' },
  { id: 'builtin:kdj_k_m', source: { type: 'builtin', key: 'kdj_k_m' }, label: '月KDJ-K', visible: false, align: 'right' },
  { id: 'builtin:kdj_d_m', source: { type: 'builtin', key: 'kdj_d_m' }, label: '月KDJ-D', visible: false, align: 'right' },
  { id: 'builtin:kdj_j_m', source: { type: 'builtin', key: 'kdj_j_m' }, label: '月KDJ-J', visible: false, align: 'right' },
  // 信号 & 图表
  { id: 'builtin:signals', source: { type: 'builtin', key: 'signals' }, label: '信号', visible: true, align: 'left' },
  { id: 'builtin:candle', source: { type: 'builtin', key: 'candle' }, label: '日k', visible: false, align: 'center' },
  { id: 'builtin:intraday', source: { type: 'builtin', key: 'intraday' }, label: '分时', visible: false, align: 'center' },
  // 财务指标 (与自选页对齐; 当前后端 enriched 未返回这些字段，默认隐藏)
  { id: 'builtin:eps', source: { type: 'builtin', key: 'eps' }, label: 'EPS', visible: false, align: 'right' },
  { id: 'builtin:bps', source: { type: 'builtin', key: 'bps' }, label: 'BPS', visible: false, align: 'right' },
  { id: 'builtin:roe', source: { type: 'builtin', key: 'roe' }, label: 'ROE', visible: false, align: 'right' },
  { id: 'builtin:pe_ttm', source: { type: 'builtin', key: 'pe_ttm' }, label: 'PE(TTM)', visible: true, align: 'right' },
  { id: 'builtin:pb', source: { type: 'builtin', key: 'pb' }, label: 'PB', visible: false, align: 'right' },
  { id: 'builtin:gross_margin', source: { type: 'builtin', key: 'gross_margin' }, label: '毛利率', visible: false, align: 'right' },
  { id: 'builtin:net_margin', source: { type: 'builtin', key: 'net_margin' }, label: '净利率', visible: false, align: 'right' },
  { id: 'builtin:revenue_yoy', source: { type: 'builtin', key: 'revenue_yoy' }, label: '营收增速', visible: false, align: 'right' },
  { id: 'builtin:net_income_yoy', source: { type: 'builtin', key: 'net_income_yoy' }, label: '净利增速', visible: false, align: 'right' },
  { id: 'builtin:debt_ratio', source: { type: 'builtin', key: 'debt_ratio' }, label: '负债率', visible: false, align: 'right' },
]

export const SCREENER_COLUMN_GROUPS: ColumnGroup[] = [
  { id: 'core', label: '核心', icon: '🎯', keys: ['strategies', 'score', 'signals'] },
  { id: 'price', label: '价格', icon: '💰', keys: ['price', 'pct', 'change_amount', 'amplitude'] },
  { id: 'volume', label: '成交', icon: '📊', keys: ['turnover', 'amount', 'float_val', 'vol_ratio', 'annual_vol'] },
  { id: 'ma', label: '均线', icon: '📈', keys: ['ma5', 'ma10', 'ma20', 'ma60'] },
  { id: 'range', label: '区间', icon: '📏', keys: ['high_60d', 'low_60d'] },
  { id: 'tech', label: '技术指标', icon: '🔬', keys: ['rsi6', 'rsi14', 'rsi24', 'macd_dif', 'macd_dea', 'macd_hist', 'kdj_k', 'kdj_d', 'kdj_j', 'boll_upper', 'boll_lower', 'atr14', 'vol_ma5', 'vol_ma10'] },
  { id: 'momentum', label: '动量', icon: '🚀', keys: ['momentum_5d', 'momentum_10d', 'momentum_20d', 'momentum_30d', 'momentum_60d'] },
  { id: 'abnormal', label: '异动', icon: '⚡', keys: ['deviate_3d', 'deviate_10d', 'deviate_30d'] },
  { id: 'limit', label: '连板', icon: '🔥', keys: ['limit_ups', 'limit_downs'] },
  {
    id: 'selection',
    label: '补充',
    icon: '🧩',
    keys: [
      'northbound_hold_pct', 'holder_num', 'holder_change_pct', 'index_membership',
      'next_unlock_date', 'unlock_float_ratio', 'weekly_change_pct', 'monthly_change_pct',
      'up_streak', 'down_streak', 'macd_hist_w', 'macd_dif_w', 'macd_dea_w', 'kdj_k_w', 'kdj_d_w', 'kdj_j_w',
      'macd_hist_m', 'macd_dif_m', 'macd_dea_m', 'kdj_k_m', 'kdj_d_m', 'kdj_j_m',
    ],
  },
  { id: 'signal', label: '信号', icon: '📡', keys: ['signals', 'candle', 'intraday'] },
  { id: 'finance', label: '财务', icon: '📋', keys: ['eps', 'bps', 'roe', 'pe_ttm', 'pb', 'gross_margin', 'net_margin', 'revenue_yoy', 'net_income_yoy', 'debt_ratio'] },
]

export async function saveScreenerColumnConfig(columns: ColumnConfig[]): Promise<void> {
  const saveable = serializeColumns(columns)
  storage.screenerResultColumns.set(saveable)
  try {
    const { api } = await import('@/lib/api')
    await api.updateScreenerResultColumns(saveable)
  } catch {
    // 后端不可用时 localStorage 仍有效
  }
}

export async function loadScreenerColumnConfig(): Promise<ColumnConfig[]> {
  try {
    const { api } = await import('@/lib/api')
    const res = await api.screenerResultColumns()
    if (res.columns && res.columns.length > 0) {
      const merged = mergeColumns(res.columns, SCREENER_BUILTIN_COLUMNS)
      storage.screenerResultColumns.set(serializeColumns(merged))
      return merged
    }
  } catch {
    // 后端不可用，继续尝试 localStorage
  }

  const saved = storage.screenerResultColumns.get([]) as ColumnConfig[]
  if (saved.length > 0) return mergeColumns(saved, SCREENER_BUILTIN_COLUMNS)

  return [...SCREENER_BUILTIN_COLUMNS]
}
