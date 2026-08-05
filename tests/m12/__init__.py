"""M12 — Property-based tests for the publish / release gate surfaces.

The M5 publish + M5 release-gate surface has a single failure
mode that has bitten us before: the gate is correct on the
inputs the test enumerates but a new combination slips through.
These property-based tests fuzz the inputs within the manifest
constraints and pin the invariants the gate MUST hold.
"""
