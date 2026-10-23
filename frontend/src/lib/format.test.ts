// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { cnDateTimeFromUtc, fmtBigNum } from './format'

// 后端自选 added_at 是 datetime.utcnow() 写入的 naive UTC 串 (无 Z 后缀)。
// 若按 JS 默认的本地时区解析, 非北京时区会显示错日; 北京时间 00:00-08:00 加入的
// 记录则会被显示成前一天。以下用例在任何时区的机器上都必须通过。
describe('cnDateTimeFromUtc: naive UTC 串 → 北京时间', () => {
  it('跨 UTC 日界: 北京凌晨加入的不能显示成前一天', () => {
    // UTC 09-18 20:30 = 北京 09-19 04:30
    expect(cnDateTimeFromUtc('2026-09-18T20:30:00')).toBe('2026-09-19 04:30:00')
  })

  it('常规时段换算 (+8 小时)', () => {
    expect(cnDateTimeFromUtc('2026-09-19T03:21:07')).toBe('2026-09-19 11:21:07')
  })

  it('北京午夜显示 00:00 而非 24:00', () => {
    expect(cnDateTimeFromUtc('2026-09-18T16:00:00')).toBe('2026-09-19 00:00:00')
  })

  it('已带时区标记的串按原语义解析', () => {
    expect(cnDateTimeFromUtc('2026-09-18T20:30:00Z')).toBe('2026-09-19 04:30:00')
    expect(cnDateTimeFromUtc('2026-09-19T04:30:00+08:00')).toBe('2026-09-19 04:30:00')
  })

  it('空值与非法值返回空串 (不抛异常、不产生 Invalid Date)', () => {
    for (const v of ['', '   ', null, undefined, 'garbage', 'not-a-date']) {
      expect(cnDateTimeFromUtc(v)).toBe('')
    }
  })
})

// 负数金额是常态: 归母净利润亏损、板块净流出、市值口径的下修差额。
// 早期实现拿有符号值直接比 `v >= 1e8`, 负数永不命中, 全部退化成裸数字 ——
// 与同量级正数差 8 个数量级, 报表和图上完全无法对齐。
describe('fmtBigNum: 按绝对值分级并保留符号', () => {
  it('负数与同量级正数用同一档单位', () => {
    for (const [v, want] of [
      [-1_200_000_000, '-12.00亿'],
      [-500_000_000, '-5.00亿'],
      [-500_000_000.0, '-5.00亿'],
      [-123_456_789, '-1.23亿'],
      [500_000_000, '5.00亿'],
      [123_456_789, '1.23亿'],
    ] as const) {
      expect(fmtBigNum(v)).toBe(want)
    }
  })

  it('万亿档负数', () => {
    expect(fmtBigNum(-3_200_000_000_000)).toBe('-3.20万亿')
    expect(fmtBigNum(3_200_000_000_000)).toBe('3.20万亿')
  })

  it('万档负数 (无小数)', () => {
    expect(fmtBigNum(-15_000)).toBe('-2万')
    expect(fmtBigNum(-10_000)).toBe('-1万')
    expect(fmtBigNum(15_000)).toBe('2万')
  })

  it('各档边界: 差一分钱就换档', () => {
    expect(fmtBigNum(9_999)).toBe('9999')
    expect(fmtBigNum(-9_999)).toBe('-9999')
    expect(fmtBigNum(99_999_999)).toBe('10000万')
    expect(fmtBigNum(-99_999_999)).toBe('-10000万')
    expect(fmtBigNum(-100_000_000)).toBe('-1.00亿')
    expect(fmtBigNum(999_999_999_999)).toBe('10000.00亿')
    expect(fmtBigNum(-1_000_000_000_000)).toBe('-1.00万亿')
  })

  it('小额负数不带负零', () => {
    // (-0.4).toFixed(0) === '-0', 直接拼符号会渲染出 "-0"
    expect(fmtBigNum(-0.4)).toBe('0')
    expect(fmtBigNum(-0.9)).toBe('-1')
  })

  it('零与负零都输出 0', () => {
    expect(fmtBigNum(0)).toBe('0')
    expect(fmtBigNum(-0)).toBe('0')
  })

  it('缺失与非法值给 —, 不给 Infinity', () => {
    for (const v of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(fmtBigNum(v)).toBe('—')
    }
  })
})
