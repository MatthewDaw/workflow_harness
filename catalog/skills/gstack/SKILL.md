---
name: gstack
description: >-
  Fast headless browser for QA testing and site dogfooding. Navigate pages,
  interact with elements, verify state, diff before/after, take annotated
  screenshots, test responsive layouts, forms, uploads, and dialogs, and capture
  bug evidence. Use when asked to open or test a site, verify a deployment,
  dogfood a user flow, or file a bug with screenshots.
---

# gstack

Catalog entry for the **gstack** headless-browser skill so it can be enabled on a
Command HQ project. The full gstack implementation (the `browse` daemon, bins, and
generated docs) ships with the gstack plugin under the developer's `~/.claude`;
this SKILL.md is the registerable org-catalog stub that `/hq-add-skill` publishes
so the project Skills tab can opt in to it.

## What it does

Drives a fast headless Chromium for QA and dogfooding: open a URL, click/type,
read the DOM and console, diff before/after states, capture annotated
screenshots, and exercise responsive layouts, forms, uploads, and dialogs — the
evidence-producing browser loop used by `/qa`, `/review`, and `/prove-it`.

## When this runs

In the developer's claude+ session, once enabled for the project. The actual
browser automation is provided by the locally-installed gstack plugin; enabling it
here lists it among the project's skills so teammates know it's part of the
project's toolset.

## Verification

Test expectation: none — SKILL.md authoring (a catalog stub). Verified by running
`/hq-add-skill` so it registers into the org catalog, after which `gstack` appears
in the project Skills tab's combobox and can be enabled.
