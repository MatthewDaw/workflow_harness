/// <reference types="vite/client" />
//
// Wires Vite's ambient client types so `import.meta.env.VITE_*` is typed.
// Several modules (baseApi.ts, authClient.ts, AppShell.tsx) read build-time
// VITE_* env vars; without this reference tsc reports `Property 'env' does
// not exist on type 'ImportMeta'`. Vite injects these at build time, so this
// is the canonical declaration rather than a runtime shim.
//
// NOTE: this file is gitignored (.gitignore excludes packages/*/src/**/*.d.ts),
// so a fresh checkout/worktree must regenerate it before `tsc` on @harness/web.
