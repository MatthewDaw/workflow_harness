# Scripted-session fixture diffs (plan-002 U3, R7)

Unified diffs that scripted-fake session steps apply to a workspace
(`git apply`, paths relative to the workspace root). They are committed
beside the toy specs and MUST survive the *real* harness gate: the canary
tests in `tests/test_sessions.py` apply every `*.diff` here to a fresh copy
of the pinned template — `git apply --check` offline everywhere, plus the
full gate (typecheck / lint / vitest) when the template is primed — so a
template version bump that breaks a fixture fails loudly as a *fixture*
problem, not a phantom pipeline bug.

When you add a diff: target only files under the template's `src/`/`server/`
trees, keep LF line endings, and run the canary tests against the primed
template before committing.
