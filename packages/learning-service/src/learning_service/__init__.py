"""learning_service — Verified Learning Python service.

Reuses agent-families' store-free core (nli.classify, judge.run_judge) in-process.
Does NOT import store.py / vecindex.py / derive_skills (SQLite-coupled).

Deploy shape: container Lambda (model bundled in the image).
v1 trigger: user-triggered ingest job (entrypoints/ingest.py).
Deferred trigger: pull_request webhook (entrypoints/webhook.py stub).
"""
