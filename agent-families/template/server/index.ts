import { Hono } from 'hono'

import type { Db } from './db/client'
import { items } from './db/schema'

export function createApp(db: Db) {
  const app = new Hono()

  app.get('/api/health', (c) => c.json({ ok: true }))

  app.get('/api/items', (c) => c.json(db.select().from(items).all()))

  app.post('/api/items', async (c) => {
    const body = await c.req.json<{ title?: unknown }>()
    if (typeof body.title !== 'string' || body.title.length === 0) {
      return c.json({ error: 'title must be a non-empty string' }, 400)
    }
    const row = db.insert(items).values({ title: body.title }).returning().get()
    return c.json(row, 201)
  })

  return app
}
