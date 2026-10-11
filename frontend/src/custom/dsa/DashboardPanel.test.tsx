// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { DashboardBoard } from './DashboardPanel'
import { dedupeMarketReviewCards, toDashboardCard } from './dashboardHistory'
import { SAMPLE_HISTORY, SAMPLE_WATCH_POOL } from './samples'

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

const rows = dedupeMarketReviewCards(SAMPLE_HISTORY.map(row => toDashboardCard({ ...row })))
const pool = SAMPLE_WATCH_POOL.map(code => ({ code, name: code }))

async function renderBoard(overrides: Partial<Parameters<typeof DashboardBoard>[0]> = {}) {
  const dismissed: number[][] = []
  const removed: string[][] = []
  await act(async () => {
    root.render(
      <DashboardBoard
        rows={rows}
        total={rows.length}
        page={1}
        pageSize={40}
        pool={pool}
        sample
        reachable={false}
        busy={false}
        errorText=""
        onPage={() => undefined}
        onRetry={() => undefined}
        onDismiss={ids => dismissed.push(ids)}
        onDismissRead={() => undefined}
        onDismissOlder={() => undefined}
        onMarkRead={() => undefined}
        onRemovePool={codes => removed.push(codes)}
        {...overrides}
      />,
    )
  })
  return { dismissed, removed }
}

it('shows one market review per day and confirms before deleting a card', async () => {
  const { dismissed } = await renderBoard()
  const text = host.textContent || ''
  expect(text).toContain('2026-10-10 大盘复盘 收盘')
  expect(text).not.toContain('2026-10-10 大盘复盘 早盘')
  expect(text).toContain('科森科技')
  expect(text).toContain('观察池')

  const menu = [...host.querySelectorAll('button')].find(button => button.getAttribute('aria-label') === '科森科技 更多操作')
  expect(menu).toBeTruthy()
  await act(async () => {
    menu?.click()
  })
  const remove = [...host.querySelectorAll('[role="menuitem"]')].find(item => item.textContent === '删除') as HTMLButtonElement | undefined
  await act(async () => {
    remove?.click()
  })
  const dialog = host.querySelector('[role="dialog"]')
  expect(dialog?.textContent).toContain('科森科技')
  expect(dismissed).toEqual([])

  const confirm = [...(dialog?.querySelectorAll('button') ?? [])].find(button => button.textContent === '删除') as HTMLButtonElement | undefined
  await act(async () => {
    confirm?.click()
  })
  expect(dismissed).toEqual([[5]])
})

it('cancels a watch-pool delete and removes the selected codes after confirm', async () => {
  const { removed } = await renderBoard()
  const multi = [...host.querySelectorAll('button')].find(button => button.textContent === '多选')
  await act(async () => {
    multi?.click()
  })
  const box = host.querySelector('input[aria-label="选择 603626"]') as HTMLInputElement | null
  await act(async () => {
    box?.click()
  })
  const action = () => [...host.querySelectorAll('button')].find(button => button.textContent === '移出所选')
  await act(async () => {
    action()?.click()
  })
  const cancel = [...host.querySelectorAll('[role="dialog"] button')].find(button => button.textContent === '取消') as HTMLButtonElement | undefined
  await act(async () => {
    cancel?.click()
  })
  expect(removed).toEqual([])

  await act(async () => {
    action()?.click()
  })
  const confirm = [...host.querySelectorAll('[role="dialog"] button')].find(button => button.textContent === '移出') as HTMLButtonElement | undefined
  await act(async () => {
    confirm?.click()
  })
  expect(removed).toEqual([['603626']])
})

it('keeps counts in one row above the pool and does not repeat an unknown code', async () => {
  await renderBoard()
  const counts = host.querySelector('[data-decision-counts]')
  const poolTitle = [...host.querySelectorAll('h2')].find(node => node.textContent?.includes('观察池'))
  expect(counts).toBeTruthy()
  expect(counts?.className).toContain('flex')
  expect(counts?.className).not.toContain('grid-cols-3')
  expect(counts!.compareDocumentPosition(poolTitle!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

  const unknown = [...host.querySelectorAll('li')].find(item => item.querySelector('[data-pool-name]')?.textContent === '603626')
  expect(unknown?.querySelector('[data-pool-code]')).toBeNull()
  expect(unknown?.textContent?.match(/603626/g)).toEqual(['603626'])
})

it('uses the shared empty state when the pool and decisions have no rows', async () => {
  await renderBoard({ pool: [], rows: [], total: 0, sample: true, reachable: false })
  const titles = [...host.querySelectorAll('h2')].map(node => node.textContent)
  expect(titles).toContain('观察池是空的')
  expect(titles).toContain('还没有分析记录')
  expect(host.textContent).toContain('到「定时推送」写入自选列表后，每日分析会回到这里。')
  expect(host.textContent).toContain('到「个股研报」提交一只股票，或等定时任务跑完。')

  await renderBoard({ pool: [], rows: [], total: 0, sample: false, reachable: false })
  const offline = [...host.querySelectorAll('h2')].map(node => node.textContent)
  expect(offline.filter(title => title === '决策服务未连接')).toHaveLength(2)
})

it('shows the stock name, code, and A-share bull/bear quote colors', async () => {
  await renderBoard({
    pool: [
      { code: '600105', name: '永鼎股份', price: 8.42, changePctPoints: 3.25 },
      { code: '603626', name: '科森科技', price: 18.36, changePctPoints: -1.8 },
    ],
  })
  const up = [...host.querySelectorAll('li')].find(item => item.textContent?.includes('永鼎股份'))
  expect(up?.querySelector('[data-pool-name]')?.className).toContain('font-semibold')
  expect(up?.querySelector('[data-pool-code]')?.textContent).toBe('600105')
  expect(up?.querySelector('[data-pool-quote]')?.textContent).toContain('8.42')
  expect(up?.querySelector('[data-pool-quote]')?.textContent).toContain('+3.25%')
  expect(up?.querySelector('[data-pool-quote]')?.className).toContain('text-bull')

  const down = [...host.querySelectorAll('li')].find(item => item.textContent?.includes('科森科技'))
  expect(down?.querySelector('[data-pool-quote]')?.textContent).toContain('-1.80%')
  expect(down?.querySelector('[data-pool-quote]')?.className).toContain('text-bear')
})
