import { expect, test } from 'vitest'

import { createDb } from './db/client'
import { createApp } from './index'

test('GET /api/health reports ok', async () => {
  const app = createApp(createDb(':memory:'))
  const res = await app.request('/api/health')
  expect(res.status).toBe(200)
  expect(await res.json()).toEqual({ ok: true })
})

test('items round-trip through the API', async () => {
  const app = createApp(createDb(':memory:'))
  const created = await app.request('/api/items', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ title: 'first' }),
  })
  expect(created.status).toBe(201)
  expect(await created.json()).toEqual({ id: 1, title: 'first' })
  const listed = await app.request('/api/items')
  expect(listed.status).toBe(200)
  expect(await listed.json()).toEqual([{ id: 1, title: 'first' }])
})

test('POST /api/items rejects a missing title', async () => {
  const app = createApp(createDb(':memory:'))
  const res = await app.request('/api/items', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({}),
  })
  expect(res.status).toBe(400)
})
