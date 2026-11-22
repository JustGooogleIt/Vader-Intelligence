"""Kalshi Trade API v2 live exchange adapter (roadmap F). DISABLED BY DEFAULT.

Specification: `docs/specs/live-adapter-v1.md`; API facts with sources and verification
tags: `docs/specs/kalshi-api-facts-2026-10-18.md` (cited below as "facts §n").

Nothing in this package runs on import, and no ordinary command path uses it:

- `activation.activate_live` is the only constructor of a `KalshiGateway` with a network
  transport, and it requires every gate of the specification at once;
- `dryrun.DryRunGateway` builds (and optionally signs) the exact requests and sends
  nothing;
- `client.KalshiClient` refuses any transport other than `httpx.MockTransport` unless
  the caller holds the activation key.

Modules: `auth` (signing), `secrets` (key providers), `client` (allowlisted HTTP),
`mapping` (order/record shapes), `gateway` (`ExchangeGateway` implementation), `dryrun`,
`activation` (gates), `shutdown` (kill switch). Every assumption that rests on an
UNVERIFIED or undocumented fact is listed in the docstring of the module that relies on
it and in `docs/live-canary-runbook.md`.
"""
