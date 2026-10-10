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
