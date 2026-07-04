# High-Level Goal → Architecture

**Type:** Pipeline — step 2

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Turn the high-level goal into an architecture: the right tooling and the dependencies
to build it on.

## Behavior

- Should be able to **choose the best tooling** for the goal.
- May run through an [Idea Refinement](./idea-refinement.md) loop to converge on the
  architecture.

## Input

- The executive goal statement from
  [High-level goal research](./high-level-goal-research.md).

## Output

- Chosen tooling / stack.
- A **dependency list**.
- (Implied) the architectural shape that the feature list and specs build on.

## Sub-agents (to flesh out)

- **Tooling selector** — evaluates and picks stack/frameworks for the goal.
- **Dependency planner** — concrete dependency list + versions/rationale.
- **Architecture critic** — runs the refinement loop, pressure-tests choices.

## Decisions

### Tooling: familiarity vs. best-fit
**Default to the boring/familiar stack; require a documented, concrete limitation before
adopting a net-new ecosystem tool.** (Choose Boring Technology — ~3 "innovation tokens"
per project; the real cost is long-term maintenance + ecosystem-boundary crossings, not
newness.) The tooling-selector sub-agent scores each candidate 1–3 on three axes and
picks the highest; a novel tool may only win if it scores **3 on necessity** *and* **≥2
on coherence**:

| Axis | 3 | 2 | 1 |
|---|---|---|---|
| **Team familiarity** | already in use | adjacent to stack | net-new ecosystem |
| **Ecosystem coherence** | same ecosystem as majority | compatible | new boundary introduced |
| **Documented necessity** | existing stack *can't* do it | significant friction | preference-driven |

Spend a token only when the new tech *is* the core differentiator, or no boring
equivalent exists for the domain — and record the "how would we solve this *without* X?"
answer in the ADR.

### Output artifacts
Emit, not just a decision blob:
- **One ADR per stack decision** — MADR 4.0 / Nygard format (context · decision drivers ·
  considered options + rejected alternatives · decision · consequences). Run them through
  the [generic refinement loop](./idea-refinement.md) (generate → validate → revise).
- **Structured dependency list** (JSON/YAML: name, version range, justification, ADR ref).
- **C4 Level-1 Context diagram** (Mermaid/PlantUML text) always; add a **Level-2
  Container diagram** when the dependency list has >1 deployable service.
- **ADR index** (auto-generated table). Bidirectional linking: every ADR references the
  components it affects; every diagram seam references its ADR number.

> Sources: Dan McKinley "Choose Boring Technology" + Glyph "Against Innovation Tokens";
> Nygard ADRs / MADR 4.0; C4 model (Simon Brown).
