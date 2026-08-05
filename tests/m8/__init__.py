"""M8 — Production Hardening.

The M7 GA gate is satisfied; M8 hardens what M0–M7 produced so a
real pilot can run on it without surprises:

  * TestM8-cli:       CLI `tenant` and `publish` subcommands
                     drive the live system end-to-end.
  * TestM8-publish:   Live E2E — tenant A publishes, a partner
                     subscribes through the Ingress, the Release
                     Gate validates, the audit timeline is
                     visible.
  * TestM8-perf:      EgressPEP + Ingress overhead in a hot loop.
  * TestM8-adr:       ADR-0003 documents the tenant isolation
                     decision.
"""
