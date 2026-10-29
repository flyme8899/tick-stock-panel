/**
 * 选股页 AI 策略收尾：草稿就地发布 → 入池 → 日线可跑选。
 *
 * 这些断言对应验收 G-T3 / G-T4：
 * - G-T3：research_only 时就地 publish；成功才算「可跑」；失败**不入池**。
 * - G-T4：入池；股票日线触发 runAll；分钟策略不强制跑。
 */
import { describe, expect, it, vi } from 'vitest'
import { handleScreenerAiStrategySaved, type ScreenerAiPublishDeps } from './screenerAiPublish'

type Preset = { id: string; timeframes?: string[] }

function mkDeps(over: Partial<ScreenerAiPublishDeps> & { presets?: Preset[] } = {}) {
  const calls = {
    publish: [] as string[],
    addToPool: [] as string[],
    runAll: [] as Array<{ strategyIds: string[]; date?: string }>,
    toast: [] as Array<{ message: string; type: string }>,
    fetch: 0,
  }
  const presets = over.presets ?? [{ id: 'ai_new', timeframes: ['1d'] }]
  const deps: ScreenerAiPublishDeps = {
    fetchStrategies: async () => {
      calls.fetch += 1
      return { presets }
    },
    publishStrategy: async (id: string) => {
      calls.publish.push(id)
      return { ok: true, strategy_id: id }
    },
    addToPool: (id: string) => { calls.addToPool.push(id) },
    requestRunAll: (opts) => { calls.runAll.push(opts) },
    toast: (message, type) => { calls.toast.push({ message, type }) },
    assetType: 'stock',
    asOf: '2026-10-29',
    ...over,
  }
  return { deps, calls }
}

describe('handleScreenerAiStrategySaved', () => {
  it('草稿：就地发布成功 → 入池 → 股票日线触发 runAll（G-T3 成功支 / G-T4）', async () => {
    const { deps, calls } = mkDeps({
      presets: [{ id: 'ai_new', timeframes: ['1d'] }],
    })

    const res = await handleScreenerAiStrategySaved('ai_new', true, deps)

    expect(calls.publish).toEqual(['ai_new'])
    // 发布后必须重取一次列表：publish 前的 presets 里它还是草稿
    expect(calls.fetch).toBe(1)
    expect(calls.addToPool).toEqual(['ai_new'])
    expect(calls.runAll).toEqual([{ strategyIds: ['ai_new'], date: '2026-10-29' }])
    expect(calls.toast).toEqual([{ message: 'AI 策略已发布, 可直接跑选', type: 'success' }])
    expect(res.published).toBe(true)
  })

  it('草稿：发布失败 → toast error，且**不入池、不跑选**（G-T3 失败支）', async () => {
    const { deps, calls } = mkDeps({
      publishStrategy: async () => { throw new Error('422 仅 AI 策略可经发布端点上线') },
    })

    const res = await handleScreenerAiStrategySaved('ai_new', true, deps)

    expect(calls.publish).toEqual([])   // 抛错发生在 mock 内部，未走到记录
    expect(calls.addToPool).toEqual([])
    expect(calls.runAll).toEqual([])
    // 失败不刷新列表也合理：既然不入池，多拉一次没意义
    expect(calls.fetch).toBe(0)
    expect(calls.toast).toHaveLength(1)
    expect(calls.toast[0].type).toBe('error')
    // 如实说明：草稿仍在 + 具体原因 + 补救路径（去策略池手动发布）
    expect(calls.toast[0].message).toContain('已保存为草稿')
    expect(calls.toast[0].message).toContain('自动发布失败')
    expect(calls.toast[0].message).toContain('422 仅 AI 策略可经发布端点上线')
    expect(calls.toast[0].message).toContain('策略池')
    expect(calls.toast[0].message).not.toContain('已发布, 可直接跑选')
    expect(res.published).toBe(false)
  })

  it('失败分支不抛给 Builder：错误只经 toast 呈现，回调本身正常返回', async () => {
    const { deps } = mkDeps({
      publishStrategy: async () => { throw new Error('network down') },
    })
    await expect(handleScreenerAiStrategySaved('ai_new', true, deps)).resolves.toEqual({ published: false })
  })

  it('草稿：分钟策略（timeframes 含 1m）发布成功但**不**强制 runAll（G-T4 分钟手动）', async () => {
    const { deps, calls } = mkDeps({
      presets: [{ id: 'ai_min', timeframes: ['1m'] }],
    })

    await handleScreenerAiStrategySaved('ai_min', true, deps)

    expect(calls.publish).toEqual(['ai_min'])
    expect(calls.addToPool).toEqual(['ai_min'])
    expect(calls.runAll).toEqual([])
  })

  it('草稿：ETF 资产类型不触发 runAll（只入池）', async () => {
    const { deps, calls } = mkDeps({
      assetType: 'etf',
      presets: [{ id: 'ai_new', timeframes: ['1d'] }],
    })

    await handleScreenerAiStrategySaved('ai_new', true, deps)

    expect(calls.addToPool).toEqual(['ai_new'])
    expect(calls.runAll).toEqual([])
  })

  it('草稿：asOf 为空时不带 date 传给 runAll', async () => {
    const { deps, calls } = mkDeps({
      asOf: '',
      presets: [{ id: 'ai_new', timeframes: ['1d'] }],
    })

    await handleScreenerAiStrategySaved('ai_new', true, deps)

    expect(calls.runAll).toEqual([{ strategyIds: ['ai_new'], date: undefined }])
  })

  it('非草稿：不调 publish，但仍入池 + 日线 runAll（现逻辑不变）', async () => {
    const { deps, calls } = mkDeps({
      presets: [{ id: 'manual_new', timeframes: ['1d'] }],
    })

    const res = await handleScreenerAiStrategySaved('manual_new', false, deps)

    expect(calls.publish).toEqual([])
    expect(calls.addToPool).toEqual(['manual_new'])
    expect(calls.runAll).toEqual([{ strategyIds: ['manual_new'], date: '2026-10-29' }])
    expect(calls.toast).toEqual([])
    expect(res.published).toBe(false)
  })

  it('保存后列表里找不到该策略：抛错，不入池不跑选', async () => {
    const { deps, calls } = mkDeps({ presets: [] })

    await expect(handleScreenerAiStrategySaved('ghost', false, deps))
      .rejects.toThrow(/ghost[\s\S]*未加载/)

    expect(calls.addToPool).toEqual([])
    expect(calls.runAll).toEqual([])
  })

  it('publish 走的是传入的依赖，不读全局 api（确认可注入、可测）', async () => {
    const publishStrategy = vi.fn(async () => ({ ok: true }))
    const { deps, calls } = mkDeps({ publishStrategy })

    await handleScreenerAiStrategySaved('ai_new', true, deps)

    expect(publishStrategy).toHaveBeenCalledOnce()
    expect(publishStrategy).toHaveBeenCalledWith('ai_new')
    expect(calls.addToPool).toEqual(['ai_new'])
  })
})
