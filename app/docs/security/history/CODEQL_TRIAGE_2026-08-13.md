# CodeQL triage, 2026-08-13 -- the two Critical SSRF alerts

Push #65 on `main` opened 37 alerts against the v2.15 sync. Two were Critical,
both `py/full-ssrf`, both in `backend/relay_client.py` (lines 22 and 32 as
pushed). This note covers those two. The other 35 are untouched.

---

## The short version

They were **half false positive**, and the half that was real is the half the
July triage talked itself out of.

Fixed, not dismissed.

---

## What the July verdict said

From `CODEQL_TRIAGE_2026-07-13.md`:

> the 4 SSRFs are guarded by `_validate_external_url` / upstream validation --
> do NOT push validation into relay_client (would break local/LAN Aether relays)

Both halves of that are correct on their own terms:

- `_validate_external_url(relay)` **is** called before `RelayClient` is
  constructed, at `skill_api.py:264` and `:299`. It rejects non-http(s) schemes
  and every private, loopback, link-local, reserved, multicast or unspecified
  address the hostname resolves to. So the scheme-confusion and
  point-it-at-169.254.169.254 attacks were already dead.
- Putting that policy *inside* `RelayClient` really would break things.
  `RelaySource` serves LAN and loopback relays from owner config (`main.py`'s
  source-loop), and `test_relay.py` drives the class against an in-process ASGI
  stub at `http://relay`. A public-address-only rule in the constructor breaks
  all of that.

## What it missed

**The validation result was thrown away.**

`_resolve_validated` exists specifically to hand back the IP it checked, so the
caller can dial *that address* instead of re-resolving the name. `_pinned_get`
uses it. The relay path did not -- it validated the URL, dropped the pin, and
passed the raw hostname to `httpx`, which resolved it again.

That is a DNS-rebinding TOCTOU. A name can answer with a public address for the
check and an internal one for the request a moment later. It is the exact hole
`_pinned_get` was written to close for the direct-peer path, left open on the
path next to it.

The docstring said so out loud the whole time:

```python
def _validate_external_url(url: str) -> None:
    """SSRF guard for the relay path (which routes through RelayClient rather than the
    pinned GET below). ..."""
```

That sentence was read as an explanation. It was a gap.

**Second thing, which CodeQL did not flag.** `await_response` concatenated
`request_id` into the URL path unencoded -- and `request_id` comes from the
*relay's own JSON response*. `RelaySource.serve_once` did the same with
`peer_id`. The host is fixed, so it is not SSRF, but a hostile or compromised
relay could steer the request path with a slash, query or fragment.

---

## The fix

Policy stays out of `relay_client`. The caller validates **and pins**.

1. `net_guard` gains `resolve_validated()` and `pinned_base()`, moved out of
   `skill_api`. They raise a plain `UrlNotAllowed` (a `ValueError`), never an
   `HTTPException`, so `net_guard` stays importable by non-FastAPI callers.
   There is now ONE implementation of the outbound address policy instead of
   one in `skill_api` and none anywhere else.

2. `RelayClient` / `RelaySource` take optional `host_header` and
   `sni_hostname`. Given them, requests go to the pinned IP base while the
   `Host` header preserves the original netloc (virtual hosting keeps working)
   and TLS SNI + certificate verification run against the hostname, not the IP.
   **A caller that passes a bare URL and no pin behaves exactly as before** --
   the source-loop, LAN relays and the tests are untouched.

3. `request_id` and `peer_id` are percent-encoded with `quote(..., safe="")`.

4. `skill_api` browse/fetch call `_pinned_relay()` and hand the result to
   `RelayClient`. `_pinned_get` now shares the same helper.

## Tests

- `test_relay.py`: 6 passed. Two new --
  `test_pinned_base_keeps_host_and_encodes_path` proves the pin, the Host
  header and the encoding in one round-trip; `test_unpinned_caller_unchanged`
  proves the no-pin path is unchanged.
- `test_skill_api.py`: 10 passed. `test_browse_bad_peer_graceful` had been
  **failing red since the July hardening** -- it still asserted the old
  "graceful `ok:false`" contract for `http://127.0.0.1:9/`, which is now a hard
  400. Corrected to assert the rejection (renamed
  `test_browse_private_address_rejected`) rather than relaxed, because a soft
  failure there is exactly the response an SSRF probe is looking for. Added
  `test_browse_bad_scheme_rejected`.
- Unchanged and green: `test_relay_core.py` (7), `test_skill_service.py` (11),
  `test_skill_store.py` (10), `test_skill_trust.py` (8), `test_skill_gate.py`
  (10), `test_skill_keys.py` (7), `test_aether_sim.py` (40).

## Both trees

`STAGING/VeridianAI_v2.15` and `STAGING/WinStoreApp/VeridianAI_v2.15` both
carry these five files. They were byte-identical before the change and are
byte-identical after it. Verified, not assumed -- this is the same
two-copies-one-updated trap as the portable `app.asar`.

---

## For the remaining 35

Two things worth carrying forward:

**Check the canary.** The inline `# codeql[py/full-ssrf]` marker put on
`skill_api.py` in July now sits above the `_pinned_get` object fetch. If
`skill_api` is absent from the full 37, inline suppression works on this setup
and the rest of the triage gets much cheaper. If it is present, inline does
nothing here and UI dismissal is the only route.

**"Validated upstream" is not a dismissal on its own.** Ask the second
question: was the *result* of that validation used, or only its verdict? A
check whose output is discarded before the request is made is a check with a
window after it.

# This document and all documentation has been generated by AI and Human edited.

- A Human (Todd [That's Me, the Human]) Architect/Director/Editor led AI coding
  team of multiple current leading online frontier models, and many local models
  using VeridianAI's multi-model slots with Toga (very large local model library).