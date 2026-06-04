# AI Usage Report

This document records how AI tooling was used to build **Command HQ + claude+**,
as required by the assessment's deliverables. It is written to be honest about
both where AI did the work and where a human stayed in the loop.

## Tools used

- **Claude Code** (Anthropic's agentic CLI), model **Claude Opus 4.x**, run inside
  the project's own **`claude+`** wrapper (the product being built — a per-repo
  daemon that hosts the real `claude` CLI, captures every session, and streams it
  to Command HQ). The project was, in part, **built with itself**.
- **Command HQ skills** (`.claude/skills/**`) — the bundled product skills were
  used as first-class commands during development: `/hq-update-progress`,
  `/hq-weekly-update`, `/hq-add-skill`, `/hq-create-skill`, `/hq-refresh-skills`,
  `/hq-relogin`, `/hq-startforge`/`/hq-endforge`, `/playwright-cli`.
- **Multi-agent workflows** — Claude Code's `Workflow` and subagent (`Agent`)
  tooling were used to parallelize large refactors (see below).
- **AWS CLI / git / gh** — driven by the agent with the developer's own
  credentials for deploys, DynamoDB seed/cleanup, and version control.

## How AI was used

AI was the primary author of code, tests, docs, and infrastructure, working from
natural-language intent with a human reviewing and steering at each step. Concrete
examples from the build:

- **Feature implementation.** Backend REST handlers and the React/RTK-Query web
  app (e.g. the two-tier requirements UI, remove-project capability, the
  compliance-breakdown panel), with accompanying unit tests, were generated and
  iterated by the agent.
- **Parallelized refactors via workflows.** Two multi-agent workflows were run:
  1. **Docs → HTML** — converting `docs/PRD.md` + all `docs/plans/**` from
     Markdown to HTML and teaching the backend/web to read `.md` **or** `.html`,
     fanned out as ~12 parallel agents (one per file + backend + web) with a
     verification pass.
  2. **Compliance breakdown report** — skill + web + content-population agents in
     parallel.
  Independent feature slices (e.g. the Detailed Requirements page) were also
  delegated to background subagents while the main session continued.
- **Debugging.** AI did root-cause analysis on production issues — empty
  Requirements pages (traced to a missing GitHub App on the deployed Lambda →
  added an unauthenticated public-repo read fallback), a stale RTK-Query cache, a
  latent `skillSchema.createdBy` mismatch masked by a stale build, and a
  device-token-vs-Cognito auth mismatch on the weekly route.
- **Infrastructure & deploys.** AI performed surgical production deploys (rebuild
  esbuild bundle → `aws lambda update-function-code`; `apigatewayv2 update-route`
  to change authorizers), DynamoDB catalog seed/cleanup, and the project-refresh
  calls — always with the developer's own AWS/GitHub credentials.
- **Documentation & process.** Progress auditing (`/hq-update-progress` writing
  `completion:` frontmatter + the compliance block), the weekly update, and this
  report were AI-generated.

## Human-in-the-loop / oversight

The human directed the work and made the decisions AI is not entitled to make:

- **Design forks were posed back to the human** before implementation — e.g. bundle
  membership mechanism (manifest vs. prefix vs. frontmatter), HTML render strategy,
  compliance-report storage/scope, and whether to fix-vs-work-around bugs.
- **Outward-facing / irreversible actions were confirmed** — production deploys,
  the auth-surface change on the weekly route, and project deletion were called
  out as risky and gated on explicit go-ahead.
- **Every change was verified** — unit suites were run (backend 207, web 109,
  shared 27, infra 22 green at time of writing), and deployed endpoints were
  invoked directly to confirm behavior before claiming success. Failures were
  reported honestly (e.g. the weekly 401, the schema 400) rather than papered over.

## Caveats & honesty notes

- The completion percentages and the compliance breakdown are **AI-computed
  estimates from code-vs-docs**, not independently verified figures; the
  prod-E2E "Definition of Done" hard gate remains **deferred**.
- Some production changes were applied as **surgical single-Lambda / single-route
  hot-patches** rather than a full `cdk deploy --all`; the committed infra
  (`infra/lib/api-stack.ts`) encodes the same end state, to be reconciled on the
  next full deploy.
- The unauthenticated GitHub public-repo reader is rate-limited (~60 req/hr/IP);
  it is a best-effort fallback, not a substitute for a configured GitHub App.

## Provenance

Most commits in this repository's history were authored in AI-assisted sessions
and carry a `Co-Authored-By: Claude` trailer. The git log is the detailed,
per-change record that complements this summary.
