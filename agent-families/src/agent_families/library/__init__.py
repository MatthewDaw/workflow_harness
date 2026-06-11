"""Library-side runtime machinery (Phase 3a+).

Phase 0 built the library as a write-side store (insights, skills, snapshots,
rendering, export). Phase 3a makes it *consequential* on the read side: this
subpackage is where the pipeline retrieves from the library into agent prompts.

`retrieval` (plan-004 U2): per-family query construction, max-member-cosine skill
ranking over the family's active pool with an own-skills budget prior, whole-skill
budget fill with logged drops, and mode-keyed quarantine visibility.
"""
