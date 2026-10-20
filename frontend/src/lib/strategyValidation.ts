/** 与 backend/app/strategy/validation_status.py 的展示文案一致。来源：策略体检 r2，2026-10-20。 */

export type ValidationStatus = 'reverse' | 'retired' | 'unverified' | 'reference_only'

export const REVERSE_NOTE = '历史上该信号后 20 日显著跑输行业'
export const REFERENCE_NOTE = '历史未证实，仅供参考'
export const UNVERIFIED_NOTE = '未经验证'
export const RETIRED_NOTE = '已下线，不作为买点展示，也不推送'

const LABELS: Record<ValidationStatus, string> = {
  reverse: '风险提示',
  retired: '已下线',
  unverified: '未经验证',
  reference_only: '仅供参考',
}

const NOTES: Record<ValidationStatus, string> = {
  reverse: REVERSE_NOTE,
  retired: RETIRED_NOTE,
  unverified: UNVERIFIED_NOTE,
  reference_only: REFERENCE_NOTE,
}

export function normalizeStatus(status?: string | null): ValidationStatus {
  if (status === 'reverse' || status === 'retired' || status === 'unverified' || status === 'reference_only') {
    return status
  }
  return 'reference_only'
}

export function validationLabel(status?: string | null): string {
  return LABELS[normalizeStatus(status)]
}

export function validationNote(status?: string | null, note?: string | null): string {
  const text = (note || '').trim()
  if (text) return text
  return NOTES[normalizeStatus(status)]
}

/** 反向信号按绿跌标成风险，避免和红涨买点混在一起。涨跌数字仍走 priceColorClass。 */
export function validationBadgeClass(status?: string | null): string {
  const kind = normalizeStatus(status)
  if (kind === 'reverse') return 'border-bear/40 bg-bear/10 text-bear'
  if (kind === 'retired') return 'border-border bg-elevated text-muted line-through decoration-muted/70'
  if (kind === 'unverified') return 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300'
  return 'border-border bg-elevated text-muted'
}

export function validationTextClass(status?: string | null): string {
  const kind = normalizeStatus(status)
  if (kind === 'reverse') return 'text-bear'
  if (kind === 'unverified') return 'text-amber-700 dark:text-amber-300'
  return 'text-muted'
}

export function showsBuyPoint(status?: string | null): boolean {
  const kind = normalizeStatus(status)
  return kind !== 'reverse' && kind !== 'retired'
}
