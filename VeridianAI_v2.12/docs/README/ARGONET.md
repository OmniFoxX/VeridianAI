# Argo-Net — the native mesh

Argo-Net is VeridianAI's peer-to-peer channel (it replaced BitChat). It's a
transport-agnostic mesh: **LAN** multicast works out of the box on any local
network with no dongle, no gateway, and no configuration; **BLE** adds phones
when the adapter can advertise; **Aether** (internet relay) exists but is off
by default and consent-gated on both ends. Find it under Socials → Argo-Net.

## Pairing devices — the shared mesh secret

**Public messages work out of the box.** With no configuration, every Argo-Net
device joins one **open public group** and can exchange public messages across
machines the moment they discover each other — the same zero-setup experience as
DMs. A public channel is public by design; for anything private, use a DM or a
private group.

**Private group (optional).** To make your *public/group* messages readable only
by your own circle, set the **same mesh secret** on every device (Socials →
Argo-Net → *private-group secret* → Save, then reconnect):

- Choose the secret and share it **over a channel you already trust** — a phone
  call, in person, an existing encrypted chat. The secret never crosses the mesh.
- Every device with the same secret forms a private group and reads that group's
  traffic; devices without it (or with a different one) can't.
- Change the secret on every device and reconnect to rotate the private-group key.

DMs are always private (per-recipient public-key encryption) and need no secret
either way.

### New identity

*New identity* generates a fresh keypair and fingerprint for this device. Use it
after you **Revoke my key** (below), or any time you want to start fresh. Peers
who verified your old fingerprint will need to re-verify the new one. Owner-only;
reconnect Argo-Net to apply.

## Direct messages (private, end-to-end)

Argo-Net can send a **private** message to one peer that no one else on the
mesh — not even other members who share the group secret — can read.

**How to send one:** with Argo-Net selected in the composer, a **To:** picker
appears. Leave it on *Everyone (group)* for a normal broadcast, or pick a peer
to send them a private DM. Received DMs show in the feed with a 🔒 lock and the
sender.

**How it works (the short version):** every device has an X25519 keypair
persisted in `sage_data`, and its fingerprint (the thing you compare in Verify
identity) is the hash of its public key — so the fingerprint is bound to the
key. A DM is encrypted with a fresh ephemeral key mixed with the sender's and
recipient's long-term keys, so:

- only the recipient's private key can open it (private from the whole group);
- each DM uses a unique key (a stolen sender key can't decrypt past DMs —
  forward secrecy on the sender side);
- the sender is authenticated and bound to their fingerprint (no impersonation).

You can only DM a peer once your device has learned their public key from their
capability announcement — i.e. after they've appeared on the mesh. Peers you
can't DM yet simply don't show in the To: picker.

**Honest limitation:** this is not a full double-ratchet. If a *recipient's*
long-term key is later stolen, DMs that were captured and addressed to them
could be decrypted. A ratchet (per-message key evolution on both ends) is future
work. DMs are also a per-peer private channel, not a group DM.

## Verify identity — and why you'd read a fingerprint aloud

*Verify identity* shows this node's fingerprint and each connected peer's.
Reading a fingerprint aloud only makes sense when you're **already** talking to
that person over a channel you trust — the same phone call where you agreed on
the mesh secret. You use that existing trust to confirm the new mesh link isn't
being impersonated: read the blocks to each other, confirm they match, then mark
the peer verified. Matching blocks = genuine peer; a mismatch = a possible
impostor, so don't trust it. (This is "trust on first use, verified
out-of-band" — you're bootstrapping a new trusted connection from one you
already have, exactly as you would when two friends add each other on a new
app.)

Each identity is an X25519 key (for DMs) plus an Ed25519 key (for signatures),
and the fingerprint is bound to **both** — so the fingerprint you compare truly
pins the whole identity, and no one can impersonate it or forge its revocation.

## Trust states: verify, flag, revoke

Argo-Net keeps trust **local and honest**, and only lets *self-claims*
propagate — that's the design that stops "I disagree with you" from ever
becoming "this person is malicious."

- **Mark verified / Unverify** — local. You vouch (to yourself) that you
  compared this peer's fingerprint out-of-band. Unverify withdraws it. Never
  leaves your device.
- **Flag compromised** — local, and **local only**. Marks a specific *key* as
  compromised: it shows a red warning and makes a DM to that key ask "send
  anyway?" first. It is never shared with anyone, so it cannot be abused to
  brand a peer for the whole mesh. Use it when you have reason to believe a
  *key* is compromised — not merely because you disagree with someone.
- **Revoke my key** — the only compromise signal that **propagates**, and it can
  only ever revoke your **own** key. If your device or key may be compromised,
  this publishes a signed revocation to the mesh; peers who receive it show your
  fingerprint as **⛔ REVOKED**. Because it's signed by the very key it revokes
  (and the signing key is bound into the fingerprint), no one can forge a
  revocation of *someone else's* key. After revoking, use **Reset key** to
  generate a fresh identity.

There is deliberately **no** way to broadcast an accusation about another
peer — third-party "this one is malicious" claims stay on your own device.

## Privacy & HIPAA notes

- Messages are encrypted end to end; relay nodes carry envelopes without reading
  them.
- Only SHA-256 identity **fingerprints** ride the mesh — never names or other
  identifiers.
- The mesh secret and per-device key live in `sage_data` (outside the project
  tree), never in `config.json`.
- Argo-Net spins up the radio only when you click **Connect**, never at startup.

## Troubleshooting

- **Public messages don't cross (but DMs and peers work):** you're in a private
  group on one side only — one device has a mesh secret set and the other
  doesn't (or a different one). Either clear the secret on both (open public
  group) or set the *same* secret on both, then reconnect. (Public messaging
  working with no secret at all was fixed in v2.12.18.)
- **Your own messages appear twice:** fixed in v2.12.17 (own multicast loopback
  is now dropped by originating-node fingerprint). If it recurs, restart the
  backend to load the fix.
- **No peers ever appear (and so the To: picker stays empty):** peer discovery
  is carried by UDP multicast the same way messages are. If the two machines
  never see each other, the network is dropping multicast — Windows Firewall may
  block inbound UDP on the Argo-Net port, and some access points don't forward
  multicast between wireless clients. Run the firewall script (below) on **each**
  machine, and prefer a wired/allowed network. (The announcement-framing bug
  that also prevented discovery was fixed in v2.12.18.)
- **Firewall setup (one script for both networks):**
  `docs/README/TogaAetherNets_Win_FirewallSetup/setup_firewall.ps1` now covers
  **both** Aether (TCP 8000) and Argo-Net (UDP 47490). In an elevated PowerShell:
  `\.setup_firewall.ps1 -Mode Open -Include ArgoNet` (or `-Include Both`). Run it
  on every machine that should join the mesh.
- **Nothing crosses between two machines even with the same secret:** multicast
  isn't traversing. Confirm the firewall rule (above) and that both
  machines are on a network that forwards multicast.
