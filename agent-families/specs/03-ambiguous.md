# Toy spec 03 — deliberately ambiguous (exercises assumptions[])

Users can sign in to the app. The spec deliberately does not say how — by
password, magic link, or single sign-on — nor what should happen when a
sign-in attempt fails.

## Expected outcome

The block below is the machine-readable assertion set the offline e2e suite
checks against this spec's documented scripted-fake episode (plan-002 R19):
the planner must record at least one entry in `assumptions[]` (the decision
it made instead of asking — assumption *verification* is Phase 2), surfaced
in the run's plan report. Live trajectories vary; a live smoke asserts only
the terminal state.

```json
{
  "terminal": "success",
  "tickets": 1,
  "done": 1,
  "escalated": 0,
  "blocked": 0,
  "min_assumptions": 1
}
```
