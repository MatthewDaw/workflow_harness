---
name: playwright-cli
description: >-
  Drive a browser from the terminal with the Playwright CLI — install browsers,
  open a URL, run a spec, record a flow into a generated script, take a
  screenshot, or trace a failing test. Use when the user says "/playwright-cli",
  "run a playwright test", "open this page in playwright", "record a browser
  flow", "screenshot this URL", "codegen a test", or "trace this failing e2e".
---

# /playwright-cli

Thin wrapper over the [Playwright](https://playwright.dev) command line so a
claude+ session can launch a real browser, exercise a page, and capture
evidence (screenshots, traces, generated scripts) without leaving the terminal.

## When this runs

In the developer's claude+ session, inside a connected repo, whenever the user
wants to QA a page, run/record an end-to-end test, or capture a screenshot from
a script. It shells out to `npx playwright` with the developer's own toolchain —
it never calls any HQ endpoint.

## Prerequisites (run once per machine)

```bash
# add Playwright as a dev dependency if the repo doesn't have it yet
npm i -D @playwright/test
# download the browser binaries (Chromium, Firefox, WebKit)
npx playwright install
```

On Linux CI you may also need the system libraries: `npx playwright install --deps`.

## Common commands

- **Open a URL in a headed browser** (manual poke):
  ```bash
  npx playwright open https://example.com
  ```

- **Screenshot a page** (headless, no test file):
  ```bash
  npx playwright screenshot --full-page https://example.com shot.png
  ```

- **Record a flow → generated script** (codegen): click through the page and
  Playwright writes the equivalent test as you go:
  ```bash
  npx playwright codegen https://example.com -o tests/recorded.spec.ts
  ```

- **Run the test suite** (all specs, or one file / one title):
  ```bash
  npx playwright test                      # everything
  npx playwright test tests/login.spec.ts  # one file
  npx playwright test -g "checkout flow"   # by title
  npx playwright test --headed --project=chromium   # watch it run
  ```

- **Open the HTML report** from the last run:
  ```bash
  npx playwright show-report
  ```

- **Trace a failing test** (record a trace, then inspect the timeline/DOM/network):
  ```bash
  npx playwright test --trace on
  npx playwright show-trace trace.zip
  ```

## Steps this skill follows

1. **Confirm the target.** Resolve the URL / spec file / test title from the
   prompt. If none is given and the repo has a `playwright.config.*`, default to
   `npx playwright test`.
2. **Ensure Playwright is installed.** If `npx playwright --version` fails,
   install it (`npm i -D @playwright/test && npx playwright install`) before
   proceeding; report what was installed.
3. **Run the requested action** with the matching command above. Prefer
   `--reporter=line` (or `list`) for legible terminal output; add `--trace on`
   when the user is debugging a failure.
4. **Surface the artifacts.** Report the paths of any screenshot, generated
   spec, trace zip, or HTML report produced, and summarize pass/fail counts.
5. **On failure, offer the trace.** If a test failed, re-run that one test with
   `--trace on` and point the user at `npx playwright show-trace`.

## Notes

- This skill assumes a Node/TypeScript repo (Playwright's native home). For a
  Python repo, the equivalent is `pip install pytest-playwright && playwright
  install`, then `pytest`.
- It does not start the app under test — launch the dev server first (or rely on
  the config's `webServer` block) so the target URL is reachable.
- Pure local tooling: nothing is posted to Command HQ.
