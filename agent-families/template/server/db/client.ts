import { mkdirSync } from 'node:fs'
import path from 'node:path'

import Database from 'better-sqlite3'
import { drizzle } from 'drizzle-orm/better-sqlite3'

import * as schema from './schema'

export type Db = ReturnType<typeof createDb>

export function createDb(
  dbPath: string = process.env.APP_DB_PATH ?? 'data/app.db',
) {
  if (dbPath !== ':memory:') {
    mkdirSync(path.dirname(dbPath), { recursive: true })
  }
  const sqlite = new Database(dbPath)
  sqlite.pragma('journal_mode = WAL')
  // Bootstrap DDL for the template's starter table; real schema evolution goes
  // through drizzle-kit (npm run db:generate / db:push).
  sqlite.exec(
    'CREATE TABLE IF NOT EXISTS items (id integer PRIMARY KEY AUTOINCREMENT, title text NOT NULL)',
  )
  return drizzle(sqlite, { schema })
}
