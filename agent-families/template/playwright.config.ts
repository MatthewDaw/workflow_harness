import { defineConfig } from '@playwright/test'

// Browser checks are driven by the verifier against an orchestrator-managed
// dev server (plan-002 R15); this config exists so `npm run test:e2e` works
// standalone during template maintenance. Not part of the harness gate.
export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://localhost:5173' },
  webServer: {
    command: 'npm run dev -- --strictPort --port 5173',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
  },
})
