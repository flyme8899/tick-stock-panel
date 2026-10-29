// @vitest-environment jsdom
// 批次 20261029gaisave (P-gaisave · 文案子批)
//
// #251 之后，Builder 里点「保存」＝就地 publish + 入池（见 screenerAiPublish.ts）。
// 但界面上还写着「保存策略」，迭代说明更直白地说「仍需你点『保存策略』加入策略池」——
// 行为已经闭环、话术还停在旧世界。这个测例只钉文案，不重复测 publish 行为
// （那部分在 screenerAiPublish.test.ts）。
//
// 断言分两层：静态扫描组件源码（钉住三处文案，不受 Dialog 打开态/step 影响），
// 再加一次真实渲染（确认 step2 渲染出来时按钮上确实是这几个字）。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrategyBuilderDialog } from './StrategyBuilderDialog'

// Dialog 打开时会探 AI 是否可用；文案与它无关，给个最小桩避免真实请求。
vi.mock('@/lib/api', () => ({
  api: {
    strategyAiStatus: async () => ({ configured: true }),
  },
  friendlyStreamError: (e: unknown) => String(e),
}))

const SOURCE = readFileSync(resolve(__dirname, 'StrategyBuilderDialog.tsx'), 'utf8')

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

async function renderDialog(mode: 'create' | 'modify' = 'create') {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  root = createRoot(container)
  await act(async () => {
    root!.render(<StrategyBuilderDialog open onClose={() => {}} mode={mode} />)
  })
}

beforeEach(() => {
  container.innerHTML = ''
  localStorage.clear()
})

afterEach(async () => {
  await act(async () => { root?.unmount() })
  root = null
})

describe('StrategyBuilderDialog 保存文案对齐 #251', () => {
  it('AI step2 主按钮写「保存并发布」，旧的「保存策略」不再作为按钮文案', () => {
    // 主按钮：saving 分支保留「保存中...」，否则一律「保存并发布」
    expect(SOURCE).toContain("{saving ? '保存中...' : '保存并发布'}")
    // 旧的主路径文案不该再有；「保存修改 / 保存自定义策略」属自定义页签，另行判定
    expect(SOURCE).not.toContain(": '保存策略'}")
  })

  it('迭代说明去掉「仍需你点『保存策略』加入策略池」，改为发布/入池真话', () => {
    expect(SOURCE).toContain('保存后会发布并加入策略池')
    expect(SOURCE).not.toContain('仍需你点「保存策略」加入策略池')
    // 顺手确认那句还留着「最多迭代 4 轮、耗时更长」的原有承诺，不是整句被换掉
    expect(SOURCE).toContain('最多迭代 4 轮，耗时更长')
  })

  it('修改区说明与主按钮语义一致（点击「保存并发布」）', () => {
    expect(SOURCE).toContain('确认无误后点击「保存并发布」')
    expect(SOURCE).not.toContain('确认无误后点击「保存策略」')
  })

  it('modify 模式的「保存修改」未被误伤', () => {
    expect(SOURCE).toContain("mode === 'modify' ? '保存修改'")
  })

  it('真实渲染：AI 迭代开说明可见，且不含误导句', async () => {
    await renderDialog()
    const toggle = Array.from(container.querySelectorAll('button'))
      .find(b => (b.textContent ?? '').includes('AI 迭代'))
    expect(toggle, '找不到「AI 迭代」开关').toBeTruthy()
    await act(async () => { toggle!.click() })

    expect(container.textContent).toContain('保存后会发布并加入策略池')
    expect(container.textContent).not.toContain('仍需你点「保存策略」加入策略池')
  })
})
