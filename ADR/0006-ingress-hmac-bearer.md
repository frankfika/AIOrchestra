# ADR-0006 — M5 Ingress uses HMAC bearer tokens, not OAuth / JWT

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: M5 PUB-002, SPEC §0.4 M5, AGENTS.md §2

## Context

When a partner calls a published capability, the Ingress layer decides
whether the call is allowed. The token must prove *who* the partner is and
*what* they can do. Two paths:

  * **OAuth 2.0 / JWT** — partner brings an ID token from their IdP; the
    Ingress validates the signature against the IdP's JWKS. Standard
    for SaaS APIs.
  * **HMAC bearer** — partner brings a base64url-encoded payload +
    HMAC-SHA-256 signature. The Ingress validates against a
    per-tenant key it shares with the partner at issue time.

## Decision

The dev path uses HMAC bearer tokens (`orchestra.publishing.ingress.Ingress`).
The token shape is `base64url(json_payload) + "." + hex(HMAC(secret, payload))`,
matching the Stripe / GitHub webhook convention so a partner's existing
verifier code transfers with one variable rename. The M6 production swap
plugs in an OIDC-aware backend behind the same
`BearerToken.from_dict` interface; the swap is a config change, not a
re-implementation.

## Consequences

  * **+** Zero IdP dependency in the dev path. A partner who doesn't
    have an IdP (the pilot case) gets a token from the orchestra
    `orchestra tenant create` / `orchestra publish create` CLI.
  * **+** Token verification is a single HMAC + a `json.loads`. No JWKS
    cache, no clock-skew handling, no `aud` mismatch. The dev path
    verifies a token in microseconds; the M8 perf benchmark records
    it.
  * **+** The secret never leaves the partner's infra — Orchestra
    hands it to the partner out-of-band, the partner uses it to mint
    tokens locally.
  * **−** The dev path doesn't have a token-rotation story. A
    partner whose secret leaks must wait for the next "publish
    create" to revoke. M21+ candidate: a `secret rotation` CLI
    command.
  * **−** No `aud` claim validation. A partner whose secret is
    shared across multiple Orchestra instances can't tell which
    one the token was minted for. Pilot is single-instance, so
    this is a known gap.

## Alternatives considered

  * **OAuth 2.0 + JWT** — production-ready. The dev path doesn't have
    an IdP. The M6 OIDC swap is the documented path; the
    `BearerToken` interface is the seam.
  * **mTLS** — partner brings a client certificate. Strong, but
    requires a partner-side PKI that the pilot doesn't have.
  * **API key in `Authorization: Bearer` header** — simplest, but
    no signature. A leaked key gives the attacker free
    access; HMAC at least requires a per-request signature that
    can be replayed but not directly reused.
