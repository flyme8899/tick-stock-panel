// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { Schedule } from './DecisionWorkspace'

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

it('defaults the notification test dropdown to DingTalk', async () => {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <Schedule reachable={false} />
      </QueryClientProvider>,
    )
  })
  const select = host.querySelector('select[aria-label="通知渠道"]')
  expect(select).toBeInstanceOf(HTMLSelectElement)
  const options = [...(select as HTMLSelectElement).options].map(option => [option.value, option.text])
  expect(options[0]).toEqual(['dingtalk', '钉钉'])
  expect((select as HTMLSelectElement).value).toBe('dingtalk')
})
