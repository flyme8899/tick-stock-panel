// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { PageHelpButton } from './PageHelpButton'

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

async function render(topicId: string) {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <PageHelpButton topicId={topicId} />
      </MemoryRouter>,
    )
  })
}

it('opens a short summary and links to the full section', async () => {
  await render('hot-events')
  const button = host.querySelector('button')
  expect(button?.getAttribute('aria-label')).toBe('热门事件使用说明')
  expect(host.textContent).not.toContain('查看完整说明')

  await act(async () => {
    button?.click()
  })

  expect(document.body.textContent).toContain('加红稿在热度里按 2.5 条计')
  const link = document.body.querySelector('a[href="/help#hot-events"]')
  expect(link?.getAttribute('href')).toBe('/help#hot-events')
  expect(link?.textContent).toBe('查看完整说明')
})

it('renders nothing for an unknown topic', async () => {
  await render('not-a-page')
  expect(host.querySelector('button')).toBeNull()
})
