"""Delivery ledger — bootstrap from forge `builds` + build-complete consumer + period query (§5).

STAGE-OWNED: built at S3 (D2). Bar clears on a merge receipt (merge_sha OR pr_url — amended
DDR-DASH-001); a COMPLETE build with neither renders the "complete — merge unverified" named gap,
never a delivered row. The internal module/table name stays `ledger`; the UI name is "Delivered"
(the query layer is the seam where the names meet — §4.4 naming map).
"""
