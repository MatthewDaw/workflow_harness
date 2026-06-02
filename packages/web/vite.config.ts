/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Command HQ SPA build + test config (U19).
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    css: false,
  },
});
