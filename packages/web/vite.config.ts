/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Command HQ SPA build + test config (U19).
//
// In dev, `/api` is proxied to the local backend (packages/backend, run via
// `npm run dev:api`) so the SPA's REST calls hit the real handlers over an
// in-memory store instead of 404ing against the static dev server. The `/api`
// prefix is stripped so the backend sees clean paths (`/projects`, …).
const DEV_API_TARGET = process.env.VITE_DEV_API_TARGET ?? 'http://localhost:8787';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: DEV_API_TARGET,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    css: false,
  },
});
