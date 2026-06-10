import { serve } from '@hono/node-server'
import { serveStatic } from '@hono/node-server/serve-static'

import { createDb } from './db/client'
import { createApp } from './index'

const app = createApp(createDb())
// Serve the built client (vite build output) for integration/browser checks.
app.use('*', serveStatic({ root: './dist' }))

const port = Number(process.env.PORT ?? 3000)
serve({ fetch: app.fetch, port }, (info) => {
  console.log(`af-template-app listening on http://localhost:${info.port}`)
})
