---
name: founder_mode
description: >-
  Create Linear ticket and PR for experimental features after implementation. Use when the user says "/founder_mode" or asks to run the founder mode workflow.
---

you're working on an experimental feature that didn't get the proper ticketing and pr stuff set up.

assuming you just made a commit, here are the next steps:

1. get the sha of the commit you just made (if you didn't make one, read `.claude/commands/commit.md` and make one)

2. read `.claude/commands/linear.md` - think deeply about what you just implemented, then create a linear ticket about what you just did, and put it in 'in dev' state - it should have ### headers for "problem to solve" and "proposed solution"
3. fetch the ticket to get the recommended git branch name
4. git checkout main
5. git checkout -b 'BRANCHNAME'
6. git cherry-pick 'COMMITHASH'
7. git push -u origin 'BRANCHNAME'
8. gh pr create --fill
9. read '.claude/commands/describe_pr.md' and follow the instructions

## Ask a human

This skill often runs autonomously (no human at the terminal). When you reach a decision only a human can make — ambiguous requirements, a product or scope judgment call, or a blocker you cannot resolve from the repo — reach a human out of band by calling the `mcp__humanlayer-contact__contact_human` tool with a clear, specific question, and wait for the reply before continuing. Only call this tool when it is available; if it is not present in this environment, fall back to your best-judgment default and do not raise an error.
