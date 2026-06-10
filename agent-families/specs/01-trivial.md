# Toy spec 01 — trivial (single ticket, happy path)

The home page shows a counter of how many bookmarks are stored, read from the
bookmarks API and rendered as plain text.

## Expected outcome

The block below is the machine-readable assertion set the offline e2e suite
(`tests/test_pipeline_e2e.py`) checks against this spec's documented
scripted-fake episode (plan-002 R19). Live trajectories vary; a live smoke
asserts only the terminal state.

```json
{
  "terminal": "success",
  "tickets": 1,
  "done": 1,
  "escalated": 0,
  "blocked": 0,
  "min_assumptions": 0
}
```
