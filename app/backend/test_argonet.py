# test_argonet.py
# Gate suite for Argo-Net integration. Run:  python test_argonet.py
# House pattern: plain asserts, no pytest, exit 1 on first failure.
# Hardware-free: LAN/BLE loops are never started; the mesh is exercised by
# handing bytes between two ArgoNode instances directly.

import asyncio
import os
import sys
import tempfile
from pathlib import Path

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, f"FAIL: {label}"
    PASS += 1
    print(f"  ok {PASS:2d} - {label}")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
print("== mesh key: sage_data placement + roundtrip ==")
import argonet_bridge

tmp = Path(tempfile.mkdtemp())
proj = tmp / "project"
data = tmp / "sage_data"
proj.mkdir()
data.mkdir()

# Point the helper's resolver at our temp dirs by shimming config + secret_locator.
import types
fake_config = types.ModuleType("config")
fake_config.DATA_DIR = data
fake_config.PROJECT_DIR = proj
sys.modules["config"] = fake_config

k1 = argonet_bridge._load_or_create_mesh_key()
ok(isinstance(k1, bytes) and len(k1) > 0, "mesh key generated")
ok((data / "argonet_mesh.key").exists(), "key written INTO sage_data (Trinity-safe)")
ok(not (proj / "argonet_mesh.key").exists(), "key NOT in project tree")
k2 = argonet_bridge._load_or_create_mesh_key()
ok(k1 == k2, "second call reuses the persisted key")

from cryptography.fernet import Fernet
f = Fernet(k1)
ok(f.decrypt(f.encrypt(b"argo")) == b"argo", "key is a valid Fernet key")

# ---------------------------------------------------------------------------
print("== shared mesh secret: deterministic key derivation ==")
d1 = argonet_bridge._derive_key_from_secret("open-mentisphere")
d2 = argonet_bridge._derive_key_from_secret("open-mentisphere")
d3 = argonet_bridge._derive_key_from_secret("different-secret")
ok(d1 == d2, "same secret derives the SAME key on both devices (mesh forms)")
ok(d1 != d3, "different secret derives a different key")
_ffd = Fernet(d1)
ok(_ffd.decrypt(_ffd.encrypt(b"x")) == b"x", "derived key is a valid Fernet key")

# Public messaging: with NO secret, nodes join a well-known OPEN public group,
# so public messages work across machines out of the box (fixed 2026-07-25).
g1 = argonet_bridge._derive_key_from_secret(argonet_bridge._DEFAULT_GROUP_SECRET)
g2 = argonet_bridge._derive_key_from_secret(argonet_bridge._DEFAULT_GROUP_SECRET)
ok(g1 == g2 and g1 != d1,
   "default public-group key is shared by all no-secret nodes (public works by default)")

# ---------------------------------------------------------------------------
print("== end-to-end: two nodes, shared key, decrypt + deliver (no network) ==")
from argonet_envelope import ArgoEnvelope, NodeCapability, Transport
from argonet_node import ArgoNode
from argonet_identity import ArgoIdentity, fingerprint_of
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

key = d1   # both nodes derived the SAME group key from the shared secret
delivered = []

# Inject DISTINCT identities (several nodes in one process must not share the
# one persisted keypair). node_id is now the fingerprint of each pubkey.
id_a = ArgoIdentity(X25519PrivateKey.generate())
id_b = ArgoIdentity(X25519PrivateKey.generate())
node_a = ArgoNode("veridianai-workstation", [Transport.LAN], relay=True, identity=id_a)
node_b = ArgoNode("veridianai-phone", [Transport.LAN], relay=True, identity=id_b)
ok(node_a.node_id != node_b.node_id, "distinct nodes have distinct fingerprints")
from argonet_identity import bind_node_id
ok(node_a.node_id == bind_node_id(id_a.public_bytes, id_a.sign_public_bytes),
   "node_id binds BOTH keys (X25519 + Ed25519)")


async def _cb(text, origin, private=False):
    delivered.append((text, origin, private))

node_b.register_message_callback(_cb)

env = ArgoEnvelope.create("the crew finds a way", node_a.node_id, key, ttl=7)
run(node_b.receive(env.to_bytes(), key))
ok(delivered and delivered[0][0] == "the crew finds a way",
   "peer decrypts a shared-key broadcast and delivers plaintext")
ok(delivered[0][1] == node_a.node_id and delivered[0][2] is False,
   "broadcast delivered with private=False, origin preserved")

# REGRESSION (2026-07-24 double-echo): a node must DROP its own envelope
# looping back (LAN multicast loopback), or the sender sees its message twice.
own_delivered = []


async def _cb_self(text, origin, private=False):
    own_delivered.append(text)

node_a.register_message_callback(_cb_self)
own_env = ArgoEnvelope.create("my own message", node_a.node_id, key, ttl=7)
run(node_a.receive(own_env.to_bytes(), key))
ok(own_delivered == [], "own envelope looping back is dropped (no double-echo)")

# dedup: same envelope again is dropped
delivered.clear()
run(node_b.receive(env.to_bytes(), key))
ok(not delivered, "duplicate envelope_id is deduped (loop guard)")

# wrong key: no delivery, no crash
delivered.clear()
env2 = ArgoEnvelope.create("secret", node_a.node_id, key, ttl=7)
run(node_b.receive(env2.to_bytes(), Fernet.generate_key()))
ok(not delivered, "envelope under a different key is not delivered (still relays silently)")

# ---------------------------------------------------------------------------
print("== DIRECT MESSAGES: end-to-end, private, authenticated ==")
# node_a discovers node_b (learns its pubkey via a capability announcement).
node_a.handle_capability_announcement(node_b.capability.to_json(), hops_away=0)
ok(node_a.get_peer(node_b.node_id) is not None
   and node_a.get_peer(node_b.node_id).pubkey == id_b.pubkey_hex,
   "peer's public key learned from its announcement")

# Identity-binding: an announcement whose keys don't bind to its node_id is
# rejected -- even the SIGNING key is bound, so an attacker can't substitute
# their own signing key (which would let them forge the victim's revocation).
spoof = NodeCapability(node_id="deadbeefdeadbeef", transports=[Transport.LAN],
                       relay=True, pubkey=id_b.pubkey_hex,
                       sign_pubkey=id_b.sign_pubkey_hex)
node_a.handle_capability_announcement(spoof.to_json(), hops_away=0)
ok(node_a.get_peer("deadbeefdeadbeef") is None,
   "announcement with mismatched keys/fingerprint is rejected (anti-spoof)")

# node_a DMs node_b; capture what node_b receives.
dm_delivered = []


async def _cb_dm(text, origin, private=False):
    dm_delivered.append((text, origin, private))

node_b._message_callbacks = []
node_b.register_message_callback(_cb_dm)

dm_env = None
_orig_broadcast = node_a._broadcast_envelope


async def _capture(env):
    global dm_env
    dm_env = env
    return True

node_a._broadcast_envelope = _capture
ok(run(node_a.send_dm("meet at the docks", node_b.node_id)) is True,
   "send_dm succeeds to a known peer")
node_a._broadcast_envelope = _orig_broadcast
ok(dm_env is not None and dm_env.recipient == node_b.node_id and dm_env.dm,
   "DM envelope addressed to recipient with a sealed box (payload empty)")
ok(dm_env.payload == "", "DM carries no group-key payload")

# node_b opens it
run(node_b.receive(dm_env.to_bytes(), key))
ok(dm_delivered and dm_delivered[0][0] == "meet at the docks"
   and dm_delivered[0][2] is True,
   "recipient opens the DM and it is flagged private")

# A THIRD node with the same GROUP key cannot read the DM (privacy from group)
id_c = ArgoIdentity(X25519PrivateKey.generate())
node_c = ArgoNode("veridianai-eve", [Transport.LAN], relay=True, identity=id_c)
c_delivered = []


async def _cb_c(text, origin, private=False):
    c_delivered.append(text)

node_c.register_message_callback(_cb_c)
run(node_c.receive(dm_env.to_bytes(), key))
ok(c_delivered == [], "a group member who is NOT the recipient cannot read the DM")

# send_dm to an unknown peer fails cleanly
ok(run(node_a.send_dm("hi", "0000unknownpeer0")) is False,
   "DM to an unknown peer (no pubkey) fails cleanly, no crash")

# ---------------------------------------------------------------------------
print("== SIGNED SELF-REVOCATION: propagate own, reject third-party ==")
# node_b discovers node_a (so it has node_a's signing key bound to its fp).
node_b.handle_capability_announcement(node_a.capability.to_json(), hops_away=0)
rev_a = id_a.make_revocation("compromised")
ok(node_b.handle_revocation(rev_a) is True,
   "a valid self-revocation is accepted and recorded")
ok(id_a.full_fingerprint in node_b.revoked,
   "revoked identity is stored by full fingerprint")
ok(node_b.handle_revocation(rev_a) is False,
   "the same revocation is not double-counted")

# A third party cannot revoke node_a: swapping in another signing key changes
# the bound fingerprint; tampering the body breaks the signature.
forged = dict(rev_a); forged["sign_pubkey"] = id_b.sign_pubkey_hex
ok(node_b.handle_revocation(forged) is False,
   "revocation with a substituted signing key is rejected")
forged2 = dict(rev_a); forged2["reason"] = "retired"
ok(node_b.handle_revocation(forged2) is False,
   "revocation with a tampered body is rejected")

# broadcast_revocation records locally even with no transports registered.
id_r = ArgoIdentity(X25519PrivateKey.generate())
node_rev = ArgoNode("rev", [Transport.LAN], relay=True, identity=id_r)
run(node_rev.broadcast_revocation(id_r.make_revocation("retired")))
ok(id_r.full_fingerprint in node_rev.revoked,
   "broadcast_revocation records our own revocation locally")

# ---------------------------------------------------------------------------
print("== REGRESSION (2026-07-24): capability announce uses ANNOUNCE framing ==")
# The bug: the announce loop reused the ENVELOPE sender, so peers dropped every
# announcement as malformed and NEVER discovered each other. The fix routes
# announcements through a dedicated ANNOUNCE sender.
from argonet_lan import LANFramer
id_x = ArgoIdentity(X25519PrivateKey.generate())
node_x = ArgoNode("x", [Transport.LAN], relay=True, identity=id_x)
_seen = {"announce": None, "env": False}


async def _fake_announce(cap_json):
    _seen["announce"] = cap_json            # a str capability, ANNOUNCE-framed
    return True


async def _fake_env(data):
    _seen["env"] = True                     # must NOT be used for announcements
    return True

node_x.register_transport(Transport.LAN, _fake_env)
node_x.register_announce_transport(Transport.LAN, _fake_announce)
run(node_x._announce_once())
ok(_seen["announce"] is not None and _seen["env"] is False,
   "announcement goes out via the ANNOUNCE sender, not the envelope sender")

# And a peer that unframes it (as the LAN receive loop does) discovers node_x.
id_y = ArgoIdentity(X25519PrivateKey.generate())
node_y = ArgoNode("y", [Transport.LAN], relay=True, identity=id_y)
framed = LANFramer.frame(_seen["announce"].encode("utf-8"), LANFramer.TYPE_ANNOUNCE)
_ptype, _cap = LANFramer.unframe(framed)
ok(_ptype == LANFramer.TYPE_ANNOUNCE, "capability framed as TYPE_ANNOUNCE on the wire")
node_y.handle_capability_announcement(_cap.decode("utf-8"), 0)
_peer = node_y.get_peer(node_x.node_id)
ok(_peer is not None and _peer.pubkey == id_x.pubkey_hex,
   "peer discovered from its announcement, with its public key (DM-ready)")

# ---------------------------------------------------------------------------
print("== reset_mesh_key: regenerate per-machine key ==")
argonet_bridge._load_or_create_mesh_key()   # ensure the file exists
ok((data / "argonet_mesh.key").exists(), "per-machine key present before reset")
ok(argonet_bridge.reset_mesh_key() is True, "reset_mesh_key() succeeds")
ok(not (data / "argonet_mesh.key").exists(), "key file removed (regenerates next connect)")
ok(argonet_bridge.reset_mesh_key() is True, "reset is safe when key already absent")

# ---------------------------------------------------------------------------
print("== rotate identity: reset regenerates a DIFFERENT keypair ==")
from argonet_identity import ArgoIdentity, IDENTITY_FILENAME, SIGNING_FILENAME
_idA = ArgoIdentity.load_or_create()
ok((data / IDENTITY_FILENAME).exists() and (data / SIGNING_FILENAME).exists(),
   "identity keys persisted (X25519 + Ed25519)")
_fpA = _idA.fingerprint
ok(ArgoIdentity.reset() is True, "identity reset() succeeds")
ok(not (data / IDENTITY_FILENAME).exists()
   and not (data / SIGNING_FILENAME).exists(), "both identity key files removed")
_idB = ArgoIdentity.load_or_create()
ok(_idB.fingerprint != _fpA,
   "a fresh identity has a DIFFERENT fingerprint (rotation works)")

# ---------------------------------------------------------------------------
print("== bridge contract: TogaMessagingAdapter surface, injected fake manager ==")
from argonet_bridge import ArgoNetBridge
from sage_messaging_adapter import TogaMessagingAdapter, ChannelMessage


class _FakeIdent:
    full_fingerprint = "f" * 64


class FakeNode:
    node_id = "abc123def456"

    def __init__(self):
        self._peers = []
        self.revoked = {}
        self.identity = _FakeIdent()

    def active_peers(self):
        return self._peers


class FakePeer:
    def __init__(self, nid, pubkey=""):
        self.node_id = nid
        self.pubkey = pubkey


class FakeManager:
    def __init__(self):
        self._running = True
        self.node = FakeNode()
        self._available_transports = [Transport.LAN]
        self.sent = []
        self.dms = []

    async def send(self, message, ttl=7):
        self.sent.append(message)
        return True

    async def send_dm(self, message, recipient_fp, ttl=7):
        self.dms.append((recipient_fp, message))
        return True

    async def stop(self):
        self._running = False


br = ArgoNetBridge({})
ok(isinstance(br, TogaMessagingAdapter), "ArgoNetBridge IS a TogaMessagingAdapter")
ok(br.available() is True, "available() True (LAN needs no special hardware)")
ok(br.EXPERIMENTAL is False, "not flagged experimental")
ok(br.PROFILE.name == "argonet", "channel profile name is 'argonet'")
ok(br.connected() is False, "not connected before start")

# Inject a fake manager (skip real network start)
br._manager = FakeManager()
br._ready = True
ok(br.connected() is True, "connected() True once manager is running")
ok(run(br.send("hello mesh")) is True, "send() routes through the manager")
ok(br._manager.sent == ["hello mesh"], "manager received the outbound (broadcast) message")

# DM routing: channel "dm:<fp>" goes to send_dm, not the group send
ok(run(br.send("psst", channel="dm:cafebabe12345678")) is True,
   "send(channel='dm:<fp>') routes to a DM")
ok(br._manager.dms == [("cafebabe12345678", "psst")],
   "DM routed privately to the recipient fingerprint")
ok(br._manager.sent == ["hello mesh"], "DM did NOT go out on the group send")

# DM inbound is flagged private and threaded per-peer
br._on_mesh_message("secret reply", "cafebabe12345678", True)
_dm_msgs = run(br.receive(timeout=0.1))
ok(_dm_msgs and _dm_msgs[0].raw.get("private") is True
   and _dm_msgs[0].channel == "dm:cafebabe12345678",
   "inbound DM flagged private and placed in its per-peer room")

# can_dm reflects whether we hold the peer's public key
br._manager.node._peers = [FakePeer("keyedpeer0001", pubkey="ab"*32),
                           FakePeer("nokeypeer0002", pubkey="")]
_ident = run(br.identity())
_bykey = {p["peer_id"]: p for p in _ident["peers"]}
ok(_bykey["keyedpeer0001"]["can_dm"] is True, "peer with a pubkey is DM-able")
ok(_bykey["nokeypeer0002"]["can_dm"] is False, "peer without a pubkey is not DM-able")

# Inbound callback -> feed drain
br._on_mesh_message("inbound ping", "peerfingerprint01")
msgs = run(br.receive(timeout=0.1))
ok(len(msgs) == 1 and isinstance(msgs[0], ChannelMessage),
   "receive() drains buffered inbound as ChannelMessage")
ok(msgs[0].platform == "argonet" and msgs[0].content == "inbound ping",
   "inbound message shaped for the socials feed")
ok(run(br.receive(timeout=0.1)) == [], "inbox emptied after drain")

# REGRESSION (2026-07-24 event-loop-starvation hang): receive() on an empty
# inbox must BLOCK for ~timeout (yielding to the loop), not busy-return. The
# router's _poll loop has no sleep of its own, so an instant-return receive()
# pegged the event loop and froze the whole app at "Connecting...". Empty
# receive should take roughly the timeout; a filled inbox should return fast.
import time as _t
_t0 = _t.monotonic()
_empty = run(br.receive(timeout=0.4))
_idle_elapsed = _t.monotonic() - _t0
ok(_empty == [] and _idle_elapsed >= 0.3,
   f"empty receive() blocks ~timeout (yields, no busy-loop) [{_idle_elapsed:.2f}s]")
br._on_mesh_message("quick", "peerX")
_t1 = _t.monotonic()
_fast = run(br.receive(timeout=5.0))
_busy_elapsed = _t.monotonic() - _t1
ok(len(_fast) == 1 and _busy_elapsed < 0.3,
   f"receive() returns promptly once a message lands [{_busy_elapsed:.2f}s]")

# peers + identity shape (feeds the existing Verify-identity UI)
br._manager.node._peers = [FakePeer("peer0000aaaa1111"), FakePeer("peer1111bbbb2222")]
prs = run(br.peers())
ok(len(prs) == 2, "peers() lists active peers")
ident = run(br.identity())
ok(ident["available"] is True and ident["fingerprint"] == "f" * 64
   and ident["node_id"] == "abc123def456",
   "identity() returns the full fingerprint + short node_id")
ok(len(ident["peers"]) == 2 and ident["peers"][0]["verified"] is True
   and ident["peers"][0]["revoked"] is False,
   "identity() reports peers encrypted, not revoked")

# disconnect tears the manager down
run(br.disconnect())
ok(br.connected() is False and br._manager is None, "disconnect() clears the manager")
ok(run(br.identity()) == {"available": False}, "identity() unavailable when down")

# ---------------------------------------------------------------------------
print("== manager threading fix present ==")
import inspect
import argonet_manager
src = inspect.getsource(argonet_manager.ArgoNetManager._on_envelope_received)
ok("call_soon_threadsafe" in src, "cross-thread-safe inbound dispatch wired")
ok("get_event_loop" not in src, "deprecated get_event_loop() removed from callback")
start_src = inspect.getsource(argonet_manager.ArgoNetManager.start)
ok("get_running_loop" in start_src, "running loop captured in start()")

print(f"\nALL {PASS} CHECKS PASSED - Argo-Net integration")
