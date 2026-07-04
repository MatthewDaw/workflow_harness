# High-Level Goal Research

**Type:** Pipeline — step 1

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Produce a very clear explanation of what the repo is trying to do at the **highest
executive level**. Similar in spirit to `/office-hours`.

Anchor on the **selling factor / promise** the product is trying to give a customer.

## Behavior

- May call a research agent for ideas on **what customers want**.

## Input

- Whatever exists: a domain, a rough pitch, an existing repo, or near-nothing.

## Output

A clear executive-level statement that includes:

- **Focus customer** — who this is for.
- **Domain understanding** — the customer's pain points / interests / goals.
- **The promise** — the core selling factor / value promised to the customer.

## Sub-agents (to flesh out)

- **Customer-needs researcher** — what does the focus customer actually want?
- **Domain expert** — pain points, jargon, landscape.
- **Promise distiller** — collapses everything into one crisp value statement.

## Research depth (decided)

**Default to a lightweight external scan** (competitor category, review forums like
G2/Reddit/Trustpilot, job postings, search intent) **before** framing — treat any
provided brief as a *hypothesis to confirm or refute*, not fact. **Skip** the external
pass only when the inputs already contain direct customer evidence (interview
transcripts, survey data, NPS verbatims with named pain points).

Trigger a **deeper** external pass when:
- the brief names a market/segment but no concrete pain, or
- there are no direct customer quotes / validated pain evidence, or
- the customer persona shifts between internal review rounds (not yet stable).

**"You have enough" signals** (lean-startup / continuous-discovery): the persona has
stabilized across rounds, research shows diminishing returns (no new insight patterns),
and the value prop articulates clearly against a *named* pain. Qualitative discovery
typically saturates around 10–20 touchpoints per cohort.

Quantify with **Opportunity Score** (`Importance + max(Importance − Satisfaction, 0)`)
when you have a job but lack segment-level sizing to prioritize — this is the output that
feeds [idea-research](./idea-research.md)'s JTBD brief and the
[goal → feature list](./high-level-goal-to-feature-list.md) prioritization.

> Sources: Teresa Torres (Continuous Discovery Habits); Steve Blank / Lean Startup
> customer discovery; Ulwick JTBD / Opportunity Scoring.
