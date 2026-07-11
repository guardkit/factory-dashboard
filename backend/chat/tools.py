"""Chat grounding tools (§3 contracts) — pure reads of the read DB, shared with the panels.

STAGE-OWNED: built at D4. Same query layer (backend/readmodel/queries.py) the panels use, so panels
and chat can never disagree.
"""
