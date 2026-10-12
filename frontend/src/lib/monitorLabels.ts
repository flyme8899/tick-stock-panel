import { cnSignal } from '@/lib/signals'

/**
 * 监控中心 / 告警弹层上的规则类型、事件类型。
 * 这些键会直接出现在行内标签和通知徽标上，不能把原始英文键露给用户。
 */
const MONITOR_TAG_LABELS: Record<string, string> = {
  strategy: '策略',
  signal: '信号',
  price: '价格',
  market: '异动',
  sector: '板块',
  abnormal: '异动',
  abnormal_up: '上涨异动',
  abnormal_down: '下跌异动',
  volume_delta: '放量',
  date: '日期',
  date_reminder: '日期',
  ladder: '封单',
  paper: '模拟盘',
  paper_fill: '模拟盘',
  buy_signal: '买入',
  sell_signal: '卖出',
  pool_entry: '进入',
  pool_exit: '移出',
  new_entry: '进入',
  dropped: '移出',
}

function isInternalKey(raw: string): boolean {
  return /^[a-z][a-z0-9_]*$/.test(raw)
}

/**
 * 监控标签：已知类型给中文，内置信号走 cnSignal，认不出的内部键用「其他」。
 * 已经是中文的规则名、自定义名称原样保留。
 */
export function monitorTagLabel(key: string | null | undefined, customNames?: Record<string, string>): string {
  const raw = (key ?? '').trim()
  if (!raw) return '其他'
  if (raw.startsWith('sector_')) return '板块'
  if (MONITOR_TAG_LABELS[raw]) return MONITOR_TAG_LABELS[raw]
  const named = cnSignal(raw, customNames)
  if (named !== raw) return named
  if (isInternalKey(raw)) return '其他'
  return raw
}

/** 异动窗口 3d/10d/30d → 3日。any 或空表示不限窗口。 */
export function abnormalWindowLabel(window: string | null | undefined): string {
  if (!window || window === 'any') return '全部窗口'
  const match = /^(\d+)d$/i.exec(window.trim())
  if (match) return `${match[1]}日窗口`
  return '其他窗口'
}

/**
 * 告警正文里的内部写法。历史记录已经落盘，展示时再转一次。
 * 例如「间隔 3s」「3d偏离」。
 */
export function monitorDisplayText(text: string | null | undefined): string {
  if (!text) return ''
  return text
    .replace(/间隔\s*(\d+(?:\.\d+)?)\s*s\b/gi, '间隔 $1秒')
    .replace(/(^|[^\w])(\d+)d(?=偏离)/gi, '$1$2日')
    .replace(/\((\d+)\s*d\)/gi, '($1日)')
}

/**
 * 行内徽标：可读的规则名原样展示；空文本或内部键改走中文标签。
 */
export function monitorBadgeText(
  text: string | null | undefined,
  fallbackKey: string | null | undefined,
  customNames?: Record<string, string>,
): string {
  const shown = monitorDisplayText(text).trim()
  if (shown && !isInternalKey(shown)) return shown
  return monitorTagLabel(fallbackKey || shown, customNames)
}
