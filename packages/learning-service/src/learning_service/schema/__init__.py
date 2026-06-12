"""schema — single-source-of-truth IDL for the harness single-table key/record schema.

This sub-package defines the IDL (``idl.py``) and the codegen entrypoint
(``codegen.py``).  Generated outputs live alongside the IDL:

  generated/py_types.py   — Python dataclasses + key builders (generated)
  generated/ts_types.ts   — TypeScript types + key helpers (generated)

CI asserts that the generated files are fresh via
``scripts/check_schema_codegen.py``; a stale output fails the build.
"""
