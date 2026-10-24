// @vitest-environment node
/**
 * 展示路径的「非有限数」兜底(B6 之后续, 批次 20261024a ①)。
 *
 * `Number.isNaN(Infinity) === false` —— 守着 NaN 的写法对 ±Infinity 完全无效, 会把它
 * 当正常数字渲染成 `"Infinity"`, 甚至染成涨色。后端 polars 的除零 / `close/prev - 1` /
 * 空集 mean 都会产出 inf, 本轮已在**响应层**统一清成 null (postgres 侧的
 * `SafeJSONResponse`), 但前端这条闸仍要可靠 —— 后端漏一个, 这里也得挡住。
 *
 * 这里只测能直接调到、且最能代表表格渲染的 `renderBuiltinDataCell`; 各页面里的
 * 展示函数形态相同 (都是 `v == null || !Number.isFinite(v)` → `—`)。
 */
import { describe, expect, it } from 'vitest'
import { renderBuiltinDataCell, RSIBadge } from './primitives'
import type { ColumnConfig } from '@/lib/list-columns'

const col = (key: string): ColumnConfig => ({
  id: key,
  label: key,
  source: { type: 'builtin', key },
  align: 'right',
}) as unknown as ColumnConfig

/** 从渲染出的 <td> 里取出文字 (去掉标签)。 */
function textOf(node: any): string {
  const kids = node?.props?.children
  if (kids == null) return ''
  return Array.isArray(kids) ? kids.join('') : String(kids)
}

describe('renderBuiltinDataCell: 非有限数显示 —', () => {
  it('kdj 列: 裸 toFixed 会输出 "Infinity", 必须被守卫挡住', () => {
    // 这几列不像 price 那样经过 fmtPrice/fmtPct (那两个已被 #195 修过), 而是直接
    // `r.kdj_k.toFixed(1)` —— Infinity.toFixed(1) === "Infinity"。守卫一漏就上屏。
    for (const [key, field] of [
      ['kdj_k', 'kdj_k'],
      ['kdj_d', 'kdj_d'],
      ['kdj_j', 'kdj_j'],
    ] as const) {
      for (const v of [Infinity, -Infinity, NaN]) {
        const out = renderBuiltinDataCell({ [field]: v }, col(key))
        expect(textOf(out), `${key}=${v}`).toBe('—')
      }
    }
  })

  it('turnover 列: `${Number(x).toFixed(2)}%` 同样会拼出 Infinity%', () => {
    for (const v of [Infinity, -Infinity, NaN]) {
      const out = renderBuiltinDataCell({ turnover_rate: v }, col('turnover'))
      expect(textOf(out)).toBe('—')
    }
    expect(textOf(renderBuiltinDataCell({ turnover_rate: 3.456 }, col('turnover')))).toBe('3.46%')
  })

  it('isBlankCell 缺口列: Infinity 必须算空, 才能落到「缺数据」而不是打印出来', () => {
    // 北向持股占比走 isBlankCell + gapKeys 判定。Infinity 若不算空, 会进
    // fmtPercentPoint 变成 "Infinity%"; 现在应判空并落到 gap 提示。
    const out = renderBuiltinDataCell(
      { northbound_hold_pct: Infinity },
      col('northbound_hold_pct'),
      { gapKeys: ['northbound_hold_pct'] },
    )
    expect(JSON.stringify(out)).not.toContain('Infinity')
  })

  it('正常值不受影响', () => {
    expect(textOf(renderBuiltinDataCell({ kdj_k: 45.67 }, col('kdj_k')))).toBe('45.7')
    expect(textOf(renderBuiltinDataCell({ pe_ttm: 12.345 }, col('pe_ttm')))).toBe('12.35')
  })

  it('缺失仍是 —', () => {
    for (const v of [null, undefined]) {
      expect(textOf(renderBuiltinDataCell({ kdj_k: v }, col('kdj_k')))).toBe('—')
    }
  })
})

describe('RSIBadge: 非有限数不染色', () => {
  it('Infinity 走 — 分支, 不会出现 "Infinity"', () => {
    const el: any = RSIBadge({ value: Infinity })
    expect(el.props.children).toBe('—')
  })

  it('正常值照旧输出一位小数', () => {
    // 用 55.25 而不是 55.55: 后者二进制不可精确表示, toFixed(1) 给的是 55.5,
    // 拿它当断言会变成在测浮点字面量而不是在测这段逻辑。
    const el: any = RSIBadge({ value: 55.25 })
    expect(String(el.props.children)).toBe('55.3')
    expect(String((RSIBadge({ value: 55 }) as any).props.children)).toBe('55.0')
  })
})

describe('红涨绿跌语义不变', () => {
  it('pct 列: 正数红 (text-bull), 负数绿 (text-bear)', () => {
    const up: any = renderBuiltinDataCell({ change_pct: 0.031 }, col('pct'))
    const down: any = renderBuiltinDataCell({ change_pct: -0.031 }, col('pct'))
    expect(up.props.className).toContain('text-bull')
    expect(down.props.className).toContain('text-bear')
  })

  it('pct 列为 Infinity 时走 text-muted, 不染色', () => {
    const inf: any = renderBuiltinDataCell({ change_pct: Infinity }, col('pct'))
    expect(inf.props.className).toContain('text-muted')
  })
})
