# Toy spec 02 — multi-ticket (dependency ordering)

The backend exposes a bookmarks API endpoint returning the stored bookmarks
as JSON.

The dashboard page lists every bookmark fetched from that endpoint, newest
first. The dashboard cannot be built before the endpoint exists.

## Expected outcome

The block below is the machine-readable assertion set the offline e2e suite
checks against this spec's documented scripted-fake episode (plan-002 R19);
the suite additionally asserts that the API ticket executes strictly before
the dashboard ticket that depends on it, and re-uses this spec for the
kill-mid-flight resume scenario. Live trajectories vary; a live smoke asserts
only the terminal state.

```json
{
  "terminal": "success",
  "tickets": 2,
  "done": 2,
  "escalated": 0,
  "blocked": 0,
  "min_assumptions": 0
}
```
