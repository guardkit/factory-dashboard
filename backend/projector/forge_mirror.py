"""Read-only forge SQLite mirror (WAL-courtesy: URI mode=ro, autocommit, <100ms txns, closed between
passes — design §4.7 / fence 4).

STAGE-OWNED: built at S2 (D1). The mirror path is EXPLICIT CONFIG, pinned on this host to the
forge-prod bind-mount /home/richardwoollcott/forge-prod-state/.forge/forge.db — NEVER the ~/.forge
default (a stale dev DB — capability note drift 1).
"""
