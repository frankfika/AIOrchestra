"""M5 (publishing-preview) test suite.

The M5 B5 gate (PUB-001, PUB-002, REL-001, PUB-003) requires:

  * Agent Cards are signed (PUB-001) and verification rejects any
    Card not in the PUBLISHED state.
  * PublishedRegistry pins versions; old versions are NOT retired
    by a new version (PUB-003).
  * Ingress.admit rejects unknown / revoked Cards, bad tokens, and
    tokens whose audience / scopes are not in the Card.
  * Kill Switch trips and admit() within the bounded-time window
    raises KillSwitchTripped.
  * Release Gate forbids free-text, errors/stacktraces, restricted
    citations, and unsourced claims past the budget.
"""
