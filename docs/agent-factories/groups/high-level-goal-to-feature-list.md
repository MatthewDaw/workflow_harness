# High-Level Goal → Feature List

**Type:** Pipeline — step 3

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Turn the high-level goal into a specific, concrete feature list.

## Output

- A **specific feature list**.
- A **fully fleshed-out wireframe** to iterate on.

## Sub-agents (to flesh out)

- **Feature decomposer** — goal → discrete features.
- **Wireframe builder** — produces the wireframe artifact.
- **Iterator** — refines the wireframe based on feedback.

## Decisions

### Wireframe format
**Canonical source of truth = a constrained JSON component tree** (structured output
against a fixed component catalog), **rendered to Tailwind HTML for human preview.** The
JSON is what the agent reads/diffs/rewrites in the iteration loop; the HTML is the view
layer humans open in a browser. Rationale: image generation isn't loop-iterable (can't
read pixels back); raw freeform JSON coordinates clutter; Tailwind HTML rendered best in
a 2024 LLM-wireframe study but JSON-as-source gives the cleanest machine iteration. Avoid
proprietary canvases (Figma Make) in a pure-agent pipeline.

### Prioritization (MVP vs. later)
**MoSCoW first, then Walking Skeleton** — both agent-executable with no usage data
(RICE needs Reach/Impact data you don't have pre-launch; reserve it for post-launch
backlog). The agent: marks each feature Must/Should/Could/Won't, then draws the **thinnest
vertical slice through the Must-haves that lets a user complete the primary JTBD
end-to-end**. Tag each feature with a one-line impact rationale (Kano intent without a
survey).

### Hand-off into [features → specs](./features-to-specs.md)
Emit a structured feature list where each feature carries: MoSCoW tag, in-MVP boolean,
the JTBD it serves, and a pointer to its node(s) in the wireframe JSON. That gives the
specs stage everything it needs to write tickets and wire dependencies.

> Sources: Sony LLM-wireframe study; Vercel json-render / A2UI; MoSCoW + Jeff Patton story
> mapping / Walking Skeleton (AltexSoft); Kano.
