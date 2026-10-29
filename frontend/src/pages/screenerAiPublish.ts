/**
 * 选股页 AI 策略「生成 → 就地发布 → 入池 → 日线可跑选」闭环。
 *
 * 为什么单独抽出来：
 * - 这段收尾逻辑要串起 publish / 列表刷新 / 入池 / 触发扫描四个副作用，
 *   埋在 `Screener.tsx` 的 JSX 回调里只能靠渲染整页来测（那一页挂着几十个
 *   query 和图表），代价高到没人愿意写，于是缺口就一直留着。
 * - 抽成显式依赖注入的纯异步函数后，每条分支都能用假依赖直接钉住，
 *   页面那边只留一层薄适配 —— 逻辑只有一份，不会出现「页面改了、测例还在
 *   测旧路径」的分叉。
 *
 * 与回测页 `StrategyBacktest.handleBuilderSaved` 的语义对齐（有意为之）：
 * 回测侧在 `research_only` 时就地 `api.strategyPublish`，成功即可直接回测；
 * 选股侧此前只 toast「请在策略池发布」然后 return —— 用户想跑一个刚生成的
 * AI 策略，得跨页去策略池点发布。本模块补齐同一件事，只是收尾动作不同：
 * 回测是「选中策略」，选股是「入池 + 日线直接跑」。
 *
 * 两处**故意不同**于回测，不要「对齐」掉：
 * 1. 发布失败：回测仍选中草稿（它只求能看能调），**选股侧直接 return**，
 *    不入池、不跑选 —— 选股页的策略池是「跑哪些」的白名单，把一份还没
 *    发布的草稿塞进去，只会在扫描时静默漏掉或报「策略未发布」。
 * 2. 成功后的列表刷新必须在 publish **之后**：发布前拉到的 presets 里
 *    该策略还是 research_only 草稿，拿它判断入池/周期会是陈旧数据。
 */

export interface ScreenerAiPublishDeps {
  /** 策略周期等元数据的来源（与选股页共用同一 query key，发布后强制重取） */
  fetchStrategies: () => Promise<{ presets: Array<{ id: string; timeframes?: string[] }> }>
  /** 把 research_only 草稿翻成公开策略 */
  publishStrategy: (id: string) => Promise<unknown>
  addToPool: (id: string) => void
  /** 触发日线扫描；分钟策略不走这里 */
  requestRunAll: (opts: { strategyIds: string[]; date?: string }) => void
  toast: (message: string, type: 'success' | 'error') => void
  /** 选股页当前资产类型；只有股票池才需要入池就立刻扫描 */
  assetType: string
  /** 选股页当前基准日；空串按未选处理 */
  asOf?: string
}

/**
 * `StrategyBuilderDialog` 保存后的收尾。
 *
 * @param id 刚保存的策略 id
 * @param researchOnly Builder 回传的草稿标记（save 默认 true，闸门本身不改）
 * @returns `published` 表示这次是否真的就地发布了草稿，仅供调用方记录/调试
 */
export async function handleScreenerAiStrategySaved(
  id: string,
  researchOnly: boolean | undefined,
  deps: ScreenerAiPublishDeps,
): Promise<{ published: boolean }> {
  const { fetchStrategies, publishStrategy, addToPool, requestRunAll, toast, assetType, asOf } = deps

  if (researchOnly) {
    try {
      await publishStrategy(id)
    } catch (e) {
      // 发布失败不是「没保存」：策略已在仓里，只是还没公开。如实说明，不假装成功。
      // 也不用「已发布」这类 success 文案 —— 那会让用户以为可以跑选，实际跑不出来。
      toast(
        `AI 策略已保存为草稿，但自动发布失败：${String((e as Error)?.message || e)}。可去策略池「AI」标签手动发布`,
        'error',
      )
      // 一旦发布失败就到此为止：不入池、不跑选。
      return { published: false }
    }
    toast('AI 策略已发布, 可直接跑选', 'success')
  }

  // 草稿刚发布完必须重取：publish 前那份 presets 里它还是草稿。
  const data = await fetchStrategies()
  if (!data.presets.some(s => s.id === id)) {
    throw new Error(`策略 ${id} 已保存但未加载，请检查策略代码`)
  }
  addToPool(id)

  // 日线策略入池即扫，省去手动点刷新；分钟策略仍由用户手动单跑。
  const preset = data.presets.find(s => s.id === id)
  if (assetType === 'stock' && preset && !preset.timeframes?.includes('1m')) {
    requestRunAll({ strategyIds: [id], date: asOf || undefined })
  }

  return { published: Boolean(researchOnly) }
}
