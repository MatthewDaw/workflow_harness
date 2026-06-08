---
name: oneshot_plan
description: >-
  Execute ralph plan and implementation for a ticket. Use when the user says "/oneshot_plan" or asks to run the oneshot plan workflow.
---

1. use SlashCommand() to call /ralph_plan with the given ticket number
2. use SlashCommand() to call /ralph_impl with the given ticket number
