import { useState } from 'react'

import { Button } from '@/components/ui/button'

export default function App() {
  const [count, setCount] = useState(0)
  return (
    <main className="mx-auto flex min-h-screen max-w-xl flex-col items-center justify-center gap-6 p-8">
      <h1 className="text-3xl font-bold tracking-tight">AF Template App</h1>
      <p className="text-muted-foreground">
        React + Vite + TypeScript + Hono + Drizzle + Tailwind + shadcn/ui
      </p>
      <Button onClick={() => setCount((c) => c + 1)}>count is {count}</Button>
    </main>
  )
}
