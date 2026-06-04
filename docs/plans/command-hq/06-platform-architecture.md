---
status: active
type: feature
created: 2026-06-02
completion: 90
feature: platform-architecture
note: Added beyond the user's five-feature list as the shared substrate. Fold/remove if undesired.
---

# Platform & Architecture (shared substrate — not a user-facing feature)

The foundation the five product features ride on. Not a user-facing feature, but
without it none of the others have a home. The key technical decisions (KTD1–12),
data model, and event schema below are the durable record — migrated here from the
original build plan so this folder is self-contained.

## Planes

1. **Capture/host** — the `claude+` daemon ([feature 4](./04-claude-cli-wrapper.md)).
2. **Transport** — outbound WebSocket from each daemon to the cloud; control
   frames back down (the **control gateway** that powers steering).
3. **State** — event store + projections (sessions, objectives roll-ups,
   weekly updates).
4. **Presentation/control** — Command HQ web + the wrapper's own tabs.

## Stack

- **AWS serverless:** API Gateway **HTTP API** (REST) + **WebSocket API** (event
  ingest + live + control); **Lambda** (TypeScript); **DynamoDB** single-table
  with GSIs; **DynamoDB Streams** drive projections (session state, objective
  roll-ups).
- **Auth:** **Cognito** for the web app (email/password self-signup **disabled**;
  accounts arrive via **Google federated** sign-in, auto-provisioned; a Cognito
  **pre-token-generation Lambda** defaults a missing `custom:org` to `personasearch`
  so a new account is authorized — `infra/lib/auth-stack.ts`); **device tokens** for
  the wrapper (`backend/src/auth/`). The API/WS JWT authorizer validates the ID token
  (audience = the app client id).
- **Forge vectors: removed.** The OpenSearch Serverless `SearchStack` and the
  `backend/src/forge/` embeddings pipeline were dropped; AgentForge distills
  client-side (see [feature 5](./05-agentforge.md)). No server-side embeddings.
- **GitHub App:** **read-only (`contents:read`)** — reads requirement docs
  (`docs/plans/` tree + `completion:` frontmatter) and git history. HQ never writes
  to GitHub. *Code:* `backend/src/github/`.
- **Web:** React + Vite + Tailwind SPA on S3 + CloudFront; **RTK Query** + a
  WebSocket middleware for live events.
- **IaC:** CDK (`infra/`).

## Key technical decisions (KTD1–12)

Migrated from the original build plan; the durable rationale for the stack.

1. **Monorepo, npm workspaces, two languages.** TS for `shared`/`backend`/`web`/`infra`; Go for `wrapper`. `packages/shared` is the single source of truth for the event schema + DTOs; the Go wrapper re-declares the envelope, kept in sync by a golden JSON fixture both sides test.
2. **Daemon + thin attach client (tmux model).** `claude+` spawns/attaches a per-repo background daemon over a local socket; sessions survive the terminal closing and are re-attachable on the same machine (single-laptop; no remote/SSH). (See [feature 4](./04-claude-cli-wrapper.md).)
3. **Capture without screen-scraping.** Three sources: PTY pass-through, tailing Claude Code's transcript JSONL, and `settings.json` hooks for lifecycle/status. Cost/tokens from the transcript.
4. **Serverless backend.** API GW HTTP API (REST) + WebSocket API (ingest + live + control); Lambda (TS); DynamoDB single-table + GSIs; Streams drive projections.
5. **Auth.** Cognito user pool for web; a device token (device-code flow, `claude+ login`) authorizes the daemon's outbound WS and scopes its data to that user. No inbound ports needed (the daemon dials out); the token carries a TTL + revoke + OS-keychain storage.
6. **Control gateway over WebSocket.** HQ steers a session by routing a control frame to the daemon's stored `connectionId` on its persistent *outbound* WS; ownership re-checked on every frame.
7. **Forge semantic search (removed, not just deferred).** Session-summary vector retrieval (OpenSearch k-NN / DynamoDB cosine) was dropped along with the `backend/src/forge/` pipeline and the `SearchStack`; AgentForge distills client-side from the working diff. No server-side embeddings.
8. **Scope model.** Three tiers `org` / `user#uid` / `proj#pid`; resolution composes all three, narrowest wins on name collision; elevate/demote rewrites the scope key. Objectives are org-global.
9. **GitHub via a GitHub App.** Reads requirement docs + git history (weekly "done") + branch/PR state — **read-only (`contents:read`), installed per-repo**. HQ reads from GitHub and never writes; `/update-progress` pushes completion numbers from the client side.
10. **Distribution.** goreleaser cross-compile + an npm wrapper package (per-platform prebuilt binaries via `optionalDependencies`) + `curl | sh` (with a published `checksums.txt` + SHA-256 verification in the installer). PTY via `creack/pty`.
11. **LLM-backed features run on the user's Claude subscription, client-side.** AgentForge distill/optimize and the Weekly conformity score run inside the claude+ PTY via Claude Code; neither the Lambda backend nor the wrapper calls a hosted model or holds model keys. See [feature 5](./05-agentforge.md).
12. **Frontend data layer.** RTK Query against REST + a thin WebSocket middleware feeding live events into the store; Tailwind; wireframe markup promoted to components.

## Data model (single-table, directional)

Single table `harness`, overloaded `PK`/`SK` + GSIs for cross-cutting queries.

| Entity | PK | SK | Notes / GSI |
|---|---|---|---|
| User | `USER#<uid>` | `PROFILE` | — |
| Project (repo) | `PROJ#<pid>` | `META` | GSI1 `USER#<uid>` → projects |
| Instance (claude+) | `PROJ#<pid>` | `INST#<host>` | live state, uptime, session count |
| Session | `PROJ#<pid>` | `SESS#<sid>` | GSI1 `live` status → cross-project live list |
| Event (append-only) | `SESS#<sid>` | `EVT#<ts>#<seq>` | TTL on old raw events optional |
| Agent | `SCOPE#<scope>` | `AGENT#<name>` | scope ∈ org / user#uid / proj#pid |
| Skill / Bundle | `SCOPE#<scope>` | `SKILL#<name>` | bundle holds member refs; nestable |
| Objective node | `ORG#<org>` | `RCDO#<path>` | tree path; roll-up % cached |
| Weekly update | `PROJ#<pid>` | `WEEK#<isoweek>` | client-posted report: `done` summary + `plan` + `conformityScore` (no ticket alignment) |

The Project record additionally stores `progressPct` + `prdGoal` +
`supportingOutcomeIds` (from the GitHub `completion:` read) with
`framingReadAt`/`framingStale` for last-known-on-failure.

*Code:* `backend/src/db/keys.ts`, `db/repo.ts`; contracts in `packages/shared/src/`.
Event records hold tool args + message content; at single-user scope, secrets-
redaction-on-capture and a mandatory event TTL are deferred hardening.

## Event schema (directional contract, shared package)

```
Event =
  | { kind: "session.start", sessionId, projectId, host, agent?, name }
  | { kind: "session.rename", sessionId, name }            // auto-generated name
  | { kind: "user.msg" | "assistant.msg", sessionId, tokens }
  | { kind: "tool.call", sessionId, tool, args summary }
  | { kind: "tool.result", sessionId, ok, ms, summary }
  | { kind: "cost.tick", sessionId, deltaUsd, totalUsd, tokens }
  | { kind: "status.change", sessionId, from, to }          // active | needs_input | idle | done
Envelope = { v, instanceId, host, ts, seq, event: Event }
```

## Monorepo layout

```
packages/shared    # event schema, DTOs, zod, scope resolution (single source of truth)
packages/backend   # TS Lambda handlers (REST + WS), DDB access layer
packages/web       # React SPA (the HQ screens)
infra              # CDK stacks
wrapper            # Go: claude+ (daemon, attach, capture, config sync)
npm/claude-plus    # npm distribution wrapper
```

## Deployed environment (prod)

Live and serving: API `https://l5edwucexb.execute-api.us-east-1.amazonaws.com`,
WebSocket `wss://fgxq7ezbl1.execute-api.us-east-1.amazonaws.com/prod`, site
`https://d13sqkbwzqe38l.cloudfront.net`, Cognito pool `us-east-1_HqxqXElfd`, org
`personasearch`. CORS is pinned to the CloudFront origin (+ the local Vite dev
origin), never `*` (`infra/lib/api-stack.ts`). The active CDK stacks are
`AuthStack`, `ApiStack`, `SiteStack`; the `SearchStack` source file remains in
`infra/lib/` but is **excluded from the synth app** (`infra/bin/infra.ts`) so a
`cdk deploy --all` can't provision OpenSearch.

## Status

- **Built:** shared contracts, DDB access layer, REST + WS handlers (including the
  control gateway, ticket subsystem **removed**), Cognito + Google-federation auth
  with the default-org Lambda, read-only GitHub integration (docs-tree +
  `completion:`), active CDK stacks, web data layer, the org-scope skill seed.
- **Open:** end-to-end deploy hardening; Streams-driven projection coverage for the
  newer features (1, 2); the in-progress heartbeat/freshness for the live list (see
  the overview's "In progress / next").
