# Security Policy

VeridianAI (Toga) is a locally-run, multi-tier AI inference platform built to
dissolve barriers to AI access for the disabled community. Because it runs
primarily on a user's own machine, most of the traditional web attack surface
doesn't apply — but a few subsystems do talk to the outside world, and those
deserve real scrutiny.

## Supported Versions

This is an actively developed, single-maintainer project. Only the latest
release receives security fixes.

| Version         | Supported           |
| ----------------|---------------------|
| v2.13+ (future+)| ✅ release+        |
| v2.12 (current) | ✅ release         |
| v2.11.11        | ❌                 |

## Scope

The following components handle untrusted or network-facing input and are
considered in-scope for security reports:

- **Aether Network** — opt-in peer mesh network (with optional WAN relay),
 Aether is fundamentally a P2P mesh—it only becomes an "internet gateway" 
 if a node opts into relaying. The threat model differs slightly (mesh
 routing vs. direct egress) Loud warnings exist by design; any bypass of
 those warnings or the opt-in gate is a valid report.
 
- **MCP server** (HTTP + stdio) — exposes Toga as a tool to external clients
  (e.g. VS Code, Continue.dev). `/metrics` and `/health` are intended for
  localhost-only loopback access only; any exposure beyond 127.0.0.1/::1 is 
  a valid report." Why: We saw in logs that accidental 0.0.0.0 binding was a
  past risk—this makes the localhost intent unmistakable for testers.
  
- **BitChat BLE gateway** (`bitchat_ble_gateway.py`, localhost:8080) — Bridges
  BLE peer messages into Toga. Malformed or malicious BLE payloads causing
  crashes, memory issues, or fragmentation exploits are in scope. BitChat peer
  identity verification — flaws allowing spoofed fingerprints (16-block SHA-256)
  or bypass of manual verification step are in-scope - this is BitChat’s 
  actual security layer—we spent time today ensuring per-peer trust survives ID 
  rotations via Noise static pubkey hashes. Calling it out directs effort where
  it counts.
  
- **ComfyUI integration** — Runs with security mitigations in place; gaps in
  those mitigations are in scope.
  
- **Fernet encryption / hash-chain log** -Fernet encryption 
  (confidentiality of stored data) and hash-chain audit log 
  (tamper evidence/integrity). Technically distinct guarantees—AES-128-CBC+HMAC 
  for confidentiality vs. Merkle-tree style chaining for integrity—but current 
  phrasing isn’t wrong, just slightly vague — any flaw that weakens integrity or
  confidentiality guarantees of stored data or the audit log is in scope.
  
- **Dependency vulnerabilities** (Dependabot alerts) — Many Dependabot alerts
 affect Electron’s build tooling only; triage confirms whether the vulnerable
 code path is reachable in VeridianAI’s runtime execution.

- **Out of scope** - Issues requiring physical access to an already-compromised
machine, or social engineering.

## Reporting a Vulnerability

Please **do not open a public GitHub issue** for security vulnerabilities.

Instead:

1. Use GitHub's **private vulnerability reporting** feature on this repo
   (Security tab → "Report a vulnerability"), **or**
2. Email: "Todd"  @  "silverfox4816@gmail.com"  Re:  "VeridianAI Security Inquiry"

Include:
- A description of the vulnerability and its potential impact.
- Steps to reproduce (a minimal repro is hugely appreciated).
- Affected version(s).

### What to expect

- **Acknowledgment** within 5 business days.
- An assessment of severity and a rough timeline for a fix.
- Credit in the release notes if you'd like it (or anonymity, your call).

This is a solo-maintained project alongside a day job of, well, building the
whole thing — response times are best-effort, not SLA-backed. Critical,
actively-exploitable issues will jump the queue.

## Dependency Vulnerabilities (Dependabot)

Most Dependabot alerts on this repo affect transitive dependencies pulled in
by Electron's build tooling, not code VeridianAI directly executes. Triage
process:

1. Confirm whether the vulnerable code path is actually reachable from
   VeridianAI's usage (many are not — see current alert triage notes).
2. If reachable, patch immediately via version bump.
3. If not reachable but a fix is available upstream, bump anyway on the next
   routine dependency update.
4. Document the reasoning in the PR that closes the alert.

## Security Practices in Place

- Fernet encryption applied across stored data.
- Hash-chain audit log for tamper detection.
- Aether Network requires explicit opt-in with visible warnings.
- Localhost-only binding intended for `/metrics`, `/health`, and the BitChat
  BLE gateway.
- WCAG 2.2 Level A/AA accessibility compliance (not a security control, but
  we're proud of it and it's not going anywhere).
