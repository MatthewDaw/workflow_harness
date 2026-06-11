"""The reflector: a mechanical attribution pass + a per-cluster counterfactual
LLM call + a batch gate (DESIGN §12).

Stage A (``stage_a``) is the deterministic attribution engine — thin LLM
micro-judgments inside a thick mechanical contract. Stage B (clustering /
counterfactual reflection), validation, and maintenance arrive in later units of
plan-004; this package grows one module per unit.
"""
