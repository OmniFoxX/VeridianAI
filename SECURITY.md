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

- **Aether Network** — opt-in, gated internet gateway. Loud warnings exist by
  design; any bypass of those warnings or the opt-in gate is a valid report.
- **MCP server** (HTTP + stdio) — exposes Toga as a tool to external clients
  (e.g. VS Code, Continue.dev). `/metrics` and `/health` are intended for
  localhost-only access; any exposure beyond localhost is a valid report.
- **BitChat BLE gateway** (`bitchat_ble_gateway.py`, localhost:8080) — bridges
  BLE peer messages into Toga. Malformed or malicious BLE payloads causing
  crashes, memory issues, or fragmentation exploits are in scope.
- **ComfyUI integration** — runs with security mitigations in place; gaps in
  those mitigations are in scope.
- **Fernet encryption / hash-chain log** — any flaw that weakens integrity or
  confidentiality guarantees of stored data or the audit log.
- **Dependency vulnerabilities** (Dependabot alerts) — see triage note below.

Out of scope: issues requiring physical access to an already-compromised
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
