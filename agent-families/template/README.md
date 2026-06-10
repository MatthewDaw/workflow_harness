# AF output-stack template (plan-002 R18)

The pinned application stack every pipeline workspace is instantiated from:
React + Vite + TypeScript (strict) + Hono + Drizzle/SQLite (better-sqlite3) +
Tailwind v4 + shadcn/ui, with Vitest and Playwright wired. All dependency
versions in `package.json` are exact pins; `package-lock.json` is the full
resolution pin and must stay committed.

## Priming (one-time, online — template maintenance)

Workspace instantiation **copies this directory, `node_modules` included**;
`npm ci` is template maintenance, never per-workspace work. After cloning the
harness repo (or bumping template versions), prime once:

```
cd agent-families/template
npm ci
```

Everything downstream is offline. An unprimed template makes
`instantiate_workspace` fail with an actionable error, and the primed-template
tests in `tests/test_workspace.py` skip.

## Gate commands (harness gate, plan-002 R12)

- `npm run typecheck` — tsc strict, app + node configs
- `npm run lint` — eslint flat config
- `npm test` — vitest run (unit/integration; node environment)

`npm run test:e2e` (Playwright) is wired but driven by the verifier against an
orchestrator-managed server, not by the gate.

## Maintenance contract

Any intentional change to template source must regenerate the content-hash pin:

```
cd agent-families
uv run python -c "from agent_families.pipeline.workspace import write_template_lock; write_template_lock()"
```

then re-verify boot from a fresh copy: `npm ci && npm run typecheck && npm run lint && npm test`.

The `.gitignore` here is load-bearing: it must cover `node_modules` and build
outputs so the orchestrator's `git clean -fd` (no `-x`) never deletes installed
dependencies in a workspace (plan-002 R3).
