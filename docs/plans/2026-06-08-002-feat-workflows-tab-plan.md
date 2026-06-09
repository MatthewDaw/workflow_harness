---
date: 2026-06-08
status: active
type: feat
---

# feat: Workflows — a registerable DAG of agents with a rerun-until-done executor

## Summary

Add **Workflows** as a first-class Command HQ catalog entity, registered and managed
*identically* to skills/agents/MCP servers. A workflow is a **DAG of catalog agents**:
each node points to an existing agent, edges are `dependsOn` dependencies, and a node
may carry a **rerun-until-done** rule — it loops until its own end-criteria is met, or
until another node (a checker agent) declares the work done. The feature spans all five
layers the sibling entities already use, plus a Go **execution engine** that actually
runs the DAG headlessly and reports live status.

Decisions locked with the user:
- **Scope:** model + register + display **and** an execution engine.
- **Skill name:** `hq-add-workflow` (repo convention; not `ac-`).
- **Sync:** full local materialization on `claude+ sync` (new `KindWorkflow`).
- **DAG view:** lightweight custom layered view using existing Tailwind/`hq-*` primitives.

The catalog/versioning envelope mirrors `agent` exactly (the versioning helpers are
already kind-generic — only a `WORKFLOW` enum value + one key builder + one switch case
are needed). The novel surface is the DAG payload, the executor, and run-status.

---

## Key Technical Decisions

- **A workflow is a DAG over *catalog agents*, not a new copy of agent logic.** Each
  node references an agent by name (the same name pointers agents use for skills). This
  is the "easy way to register agents": you pick from the existing agent catalog (and
  mint new ones inline via `/hq-add-agent` or the agent editor). Enabling a workflow on
  a project **unions every referenced agent** — and transitively their skills + MCP
  servers — into the project's enabled sets, by reusing the existing private
  `bringAgentInto` (the same machinery that makes enabling an agent pull its skills).

- **Edges are `dependsOn` per node, not a separate edges array.** Simpler to store,
  validate, and topologically sort; acyclicity is enforced in a `superRefine`. This is
  the canonical DAG encoding and maps 1:1 onto the executor's wave scheduler and the
  layered visualization.

- **Rerun-until-done has two modes, both stored in the spec:**
  - `self` — after each run the executor asks a Haiku judge "is `<endCriteria>`
    satisfied? DONE/CONTINUE", looping until DONE or `maxRuns`.
  - `declared-by` — node N reruns until node M (a checker agent) declares N's output
    done. This is the generator↔checker loop and is exactly "another agent declares
    something fully done." M's verdict gates N's loop.
  Both carry a `maxRuns` safety cap so a workflow can never loop forever.

- **The versioning system is already generic.** Per the keys/repo analysis, `WORKFLOW`
  drops into `CatalogKind`, `putNewVersion`, `listRevisions`, `setTrueVariant`,
  `truePointerKey`, `isVersionSideRecord`, and `isBuiltin` with **no changes to those
  functions** — only the `CatalogKind` union, a new `workflowKey`, and the `itemKey`
  switch case. So workflows get fork-on-edit, revisions, and promote for free.

- **No bundle concept for workflows in v1.** Agents/skills have `kind: …|bundle`; a
  workflow is itself the composition unit, so `kind` is the single literal `'workflow'`
  (the field is kept for catalog parity and future bundling). This removes the
  members/dissolve/add-member surface from the clone.

- **The executor is a standalone CLI command, not a daemon path.** Per the runtime
  analysis there is no scheduler today, and managed sessions are interactive-PTY-only —
  but the headless `claude -p` pattern already exists twice (`internal/judge`,
  `internal/title`). The executor is a new `claude+ run-workflow <name>` verb that fetches
  the workflow, topo-sorts, runs each node's agent headlessly (`claude -p`, capturing
  stdout), evaluates the rerun rule, and POSTs node status — reusing the existing device-
  token HTTP client. It never touches the PTY mux. A `CLAUDE_PLUS_WORKFLOW=1` marker is
  added to the hook bypass so node runs don't surface as phantom sessions.

- **Run status is its own DynamoDB record, polled by the web tab.** A run is
  `WORKFLOWRUN#<runId>` under the project (or scope) partition, with per-node state
  (`pending|running|looping|done|failed`, run count, last output tail). The web Workflows
  tab overlays this onto the layered DAG view and polls while a run is active.

- **Full sync materialization writes the workflow spec as JSON.** Unlike a skill
  (directory of files) or agent (markdown), a workflow is structured data, so
  `ApplyPulled` writes `~/.claude+/workflows/<name>.json`. The Go sync pipeline is
  per-type at every layer, so `KindWorkflow` is added across `Fetch`, `ReadLocal`,
  `ApplyPulled`, `Push`, `verify`, and `prune` — mirroring agents.

---

## Milestones

Each milestone is independently buildable, testable, and reviewable. M1–M3 deliver the
"register + display" product; M4–M5 deliver sync + execution.

### M1 — Data model & catalog plumbing (shared + backend + infra)

**Shared (`packages/shared/src/dto.ts`):**
- Add `workflowNodeSchema`, `workflowRerunSchema`, `workflowSchema` (mirrors `agentSchema`
  envelope: `name`, `scope`, `kind` default `'workflow'`, `description`, `createdBy`,
  `.merge(versionFieldsSchema)`), with a `superRefine` enforcing: unique node ids, every
  `dependsOn`/`declaredBy` resolves to a node id, and the graph is acyclic.
- Export `Workflow`, `WorkflowNode` types and `WORKFLOW_KINDS`.
- Add `enabledWorkflows: z.array(z.string()).default([])` to `projectSchema`.

Node shape:
```ts
workflowNodeSchema = z.object({
  id:        z.string().min(1),
  agent:     z.string().min(1),                 // name pointer to a catalog agent
  label:     z.string().default(''),
  prompt:    z.string().default(''),            // per-node task instructions
  dependsOn: z.array(z.string()).default([]),   // upstream node ids → DAG edges
  rerun:     z.object({
    mode:        z.enum(['self', 'declared-by']),
    endCriteria: z.string().default(''),        // self mode
    declaredBy:  z.string().optional(),         // declared-by mode → checker node id
    maxRuns:     z.number().int().positive().default(10),
  }).optional(),
});
```

**Keys (`packages/backend/src/db/keys.ts`):** add `'WORKFLOW'` to `CatalogKind`; add
`workflowKey(scope,name)` after `mcpServerKey`. (2 edits; all versioning helpers already
generic.)

**Repo (`packages/backend/src/db/repo.ts`):**
- `itemKey` switch: add `case 'WORKFLOW': return k.workflowKey(scope, name);`
- Add `putWorkflow` / `getWorkflow` / `deleteWorkflow` / `listWorkflows` (mirror agent CRUD).
- Add `addWorkflowToProject` / `removeWorkflowFromProject` and a private
  `bringWorkflowInto(project, name, org)` that adds to `enabledWorkflows` and, for each
  node's `agent`, calls the existing `bringAgentInto` (unioning agents → skills → MCP).
  Removal only strips `enabledWorkflows` (leaves agents/skills, mirroring agent removal).

**REST (`packages/backend/src/rest/workflows.ts`, new):** clone `agents.ts` minus the
bundle/member/dissolve branches. Routes: `GET /workflows`, `POST /workflows`,
`GET|PUT|DELETE /workflows/{name}`, `POST /workflows/{name}/promote`. Auth via
`resolveOrgCatalogAuth`, force `orgScope`, stamp `createdBy`/`baseName`, write via
`putNewVersion('WORKFLOW', …)`. Annotate nothing extra (no resolvedMembers).

**Projects REST (`packages/backend/src/rest/projects.ts`):** add dispatch for
`/projects/{id}/workflows/{name}` POST/DELETE → `enableProjectWorkflow` /
`disableProjectWorkflow` (call the repo methods above).

**Infra (`infra/lib/api-stack.ts` + `infra/scripts/bundle-backend.mjs`):**
- `bundle-backend.mjs`: add `'rest/workflows'` entry.
- `api-stack.ts`: `makeFn('RestWorkflowsFn', 'rest_workflows')` + `grantReadWrite`; register
  the `/workflows*` routes (noAuth, mirroring agents) and the two project opt-in routes
  through `projectsFn`.

**Seed (`packages/backend/src/seed/workflows.ts`, new):** `buildSeedWorkflows(org, files)`
mirroring `seed/agents.ts` (system `createdBy`, `baseName`/`variantId`/`version` stamped,
parsed through `workflowSchema`). Add an `infra/scripts/seed-workflows.mjs` script and
include workflows in `seed-all-orgs.mjs` so every org gets the starter set.

**Tests:** `packages/backend/test/workflows.test.ts` (REST CRUD, scope forcing, builtin
409, promote) and project opt-in union test; shared schema unit tests for the DAG
validators (cycle rejection, dangling dependsOn). Mirror `agents.test.ts`.

### M2 — Web Workflows tab (global catalog + editor + project sub-tab + DAG view)

**baseApi (`packages/web/src/api/baseApi.ts`):** add tag `'Workflow'`; add `getWorkflows`,
`getWorkflow`, `saveWorkflow`, `enableProjectWorkflow`, `disableProjectWorkflow`,
`promoteWorkflow`, and (M5) `getWorkflowRun` — mirroring the agent endpoints + invalidation.

**Nav + routes:**
- `components/AppShell.tsx` `NAV`: add `{ to: '/workflows', label: 'Workflows' }` after Agents.
- `screens/ProjectDetail/ProjectLayout.tsx` `SUBTABS`: add `{ to: 'workflows', label: 'Workflows' }`.
- `app/router.tsx`: top-level `workflows`, `workflows/new`, `workflows/:name/edit`; project
  sub-route `workflows`.

**Global catalog screen (`screens/Workflows/Workflows.tsx`):** clone `Agents.tsx` — grid of
workflow cards (name, node count, description, "Expand" → modal showing the layered DAG).

**Editor (`screens/Workflows/WorkflowEditor.tsx`):** clone `AgentEditor.tsx` shape:
- Add/remove nodes; per node: pick `agent` (combobox over `useGetAgentsQuery`), `label`,
  `prompt`, `dependsOn` (multi-toggle of other node ids), and an optional rerun rule
  (mode + endCriteria / declaredBy + maxRuns).
- Live client-side validation (unique ids, no cycles, resolvable refs) before save.
- Live preview using the shared DAG view component.

**DAG view (`components/WorkflowGraph.tsx`, new):** the lightweight custom layered view —
compute topological levels client-side, render each level as a row of node cards, draw SVG
connector lines for `dependsOn`, badge rerun nodes (`↻` + criteria tooltip) and render the
`declared-by` checker edge in a distinct style. Accepts optional `runState` to overlay
per-node status colors (used in M5).

**Project sub-tab (`screens/ProjectDetail/ProjectWorkflows.tsx`):** clone
`ProjectAgents.tsx` — `CatalogPicker` "+ Add to project", enabled-workflow cards (each
shows the DAG + which agents it brings), sequential `onApply`, disable button. (M5 adds a
"Run" button + live run overlay.)

**Tests:** component/RTK tests mirroring existing project sub-tab tests; a `WorkflowGraph`
topological-layout unit test.

### M3 — `hq-add-workflow` catalog skill + opt-in reference

- New `catalog/skills/hq-add-workflow/SKILL.md`, modeled on `hq-add-agent/SKILL.md`:
  gather the workflow record (nodes/agents/dependsOn/rerun, referenced agents must already
  exist in the catalog — mint via `/hq-add-agent` first), `POST /workflows`, then the
  resolve→enable→sync→verify round trip (`POST /projects/:id/workflows/<name>`,
  `claude+ sync`, assert `~/.claude+/workflows/<name>.json` exists). Reuse the shared
  `docs/superpowers/hq-catalog-opt-in.md` reference; add a workflow bullet to its verify
  step (the `~/.claude+/workflows/<name>.json` artifact).
- Register + seed per the standing rule (commit/push to GitHub main + `seed-all-orgs.mjs`).

### M4 — Full sync materialization (wrapper Go)

In `wrapper/internal/config/`:
- `claude.go`: add `KindWorkflow Kind = "workflow"`; `ApplyPulled` writes
  `~/.claude+/workflows/<name>.json`; `ReadLocal` reads that dir.
- `remote.go`: `remoteWorkflow` struct; fourth `Fetch` block (`GET /workflows`, intersect
  with `project.enabledWorkflows`); `EnabledWorkflows` on `remoteProject`; `Push` case.
- `verify.go` + `prune.go`: `KindWorkflow` cases (verify the JSON parses; prune like agents).
- Go tests mirroring the agent sync tests.

### M5 — Execution engine (wrapper Go + run-status REST + live web overlay)

**Backend run-status:**
- Schema: `workflowRunSchema` (runId, workflowName, projectId, status, per-node
  `{state, runs, outputTail}`, timestamps) in `dto.ts`.
- Keys: `workflowRunKey` (`PROJ#<projectId>` / `WORKFLOWRUN#<runId>`).
- REST (extend `workflows.ts` or `projects.ts`): `POST /workflows/{name}/runs` (create →
  runId), `POST /workflows/{name}/runs/{runId}/nodes/{nodeId}` (update node state),
  `GET /workflows/{name}/runs/{runId}` (read), `GET /workflows/{name}/runs` (list).

**Executor (wrapper):**
- New `internal/workflow/` package: spec types (mirror DTO), `TopoSort` (Kahn's algorithm
  over `dependsOn`), `runNode` (headless `claude -p` with the node's agent + prompt +
  dependency outputs as context, capturing stdout via `cmd.Output()` — the judge/title
  pattern, injectable `var runClaude` seam), and `evalRerun` (self: Haiku DONE/CONTINUE
  judge; declared-by: run the checker node, parse its verdict), each respecting `maxRuns`.
- New `cmd/claude-plus/workflow.go`: `cmdRunWorkflow(args)` — resolve repoRoot/creds like
  `cmdSync`, fetch the workflow + create a run, schedule waves (nodes whose deps are all
  done; bounded concurrency via a token-bucket like the existing judge limiter), run each
  node with its rerun loop, POST node status after each transition, finalize the run.
- `main.go`: add `case "run-workflow"` verb; add `CLAUDE_PLUS_WORKFLOW=1` to the
  `runHook` bypass (alongside `CLAUDE_PLUS_TITLE`/`CLAUDE_PLUS_JUDGE`).
- Go tests for `TopoSort`, the wave scheduler, and the rerun loop (stubbing `runClaude`).

**Web live overlay:** in `ProjectWorkflows.tsx`, a "Run" button POSTs a run and the
`WorkflowGraph` overlays `runState` from `getWorkflowRun`, polled (RTK `pollingInterval`)
while `status === 'running'`. Per-node color = state; rerun nodes show their run count.

---

## Risks & Mitigations

- **Executor is the highest-risk surface (real subprocesses, real money, loops).** Mitigate
  with mandatory `maxRuns` caps, per-node context-timeouts (judge/title precedent),
  bounded concurrency, and stubbed `runClaude` in tests. The executor lands last (M5) on
  top of a proven, independently-shipped catalog (M1–M3).
- **Deployed API lags merged code** (per memory): new `/workflows` routes 404 until
  `cdk deploy ApiStack`; the web tab shows empty state, not an error. Note in the M1 PR.
- **Seeding must hit all orgs** (per memory): wire workflows into `seed-all-orgs.mjs`, not a
  single `SEED_ORG`.
- **`declared-by` cycles:** a checker referencing a node that depends on it could deadlock
  the scheduler. The `superRefine` treats `declaredBy` as a non-DAG control edge (excluded
  from the acyclicity check) but the executor guards it with `maxRuns` so it can't hang.

## Out of scope (v1)

- Workflow *bundles* (kind union kept for future).
- Branching/conditional edges beyond `dependsOn` + rerun (no `if/else` node routing).
- Cross-project / org-wide run dashboards (runs are per-project, viewed in the sub-tab).
