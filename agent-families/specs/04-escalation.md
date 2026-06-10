# Toy spec 04 — engineered escalation (unhappy state-machine arm)

The service exposes a health endpoint that reports the build status. This
ticket is engineered to fail under the scripted episode: the worker never
produces code that satisfies the harness gate, so the Ralph cap exhausts and
the ticket escalates.

The status page renders the health endpoint's output and therefore depends on
it; once the health ticket escalates, this ticket must be blocked, never
attempted.

## Expected outcome

The block below is the machine-readable assertion set the offline e2e suite
checks against this spec's documented scripted-fake episode (plan-002 R19):
persistent gate failure exhausts the cap, the first ticket escalates, its
dependent is blocked, and the run settles `partial` (R2). Live trajectories
vary; a live smoke asserts only the terminal state.

```json
{
  "terminal": "partial",
  "tickets": 2,
  "done": 0,
  "escalated": 1,
  "blocked": 1,
  "min_assumptions": 0
}
```
