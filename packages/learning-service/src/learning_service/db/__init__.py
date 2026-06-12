"""learning_service.db — DynamoDB single-table + S3 Vectors persistence layer (F2 / MAT-149).

This package provides the boto3-backed persistence for every learning record:
  - Ideas, golden cases, anchor index, processed-PR cursor, verify-event audit
  - S3 Vectors idea + skill vector upsert/query

Design constraints (from MAT-149 / plan §Data model changes):
  - **Cross-org guard on every write** — blank org raises OrgGuardError.
  - **Conditional writes / OCC** — idea writes use the corroborationVersion token.
  - **Exclude invalidAt from current-set reads** — `list_current_ideas` filters out
    any idea where invalidAt is set; `get_idea` still returns them for history.
  - **Dependency injection** — all clients are injectable so tests never touch real AWS.
"""
