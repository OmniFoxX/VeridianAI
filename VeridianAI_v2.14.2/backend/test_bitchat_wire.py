#!/usr/bin/env python3
"""Isolated wire-format tests for the patched bitchat.py.
Stubs the heavy BLE/crypto imports so we can load the module and exercise the
pure encode/parse functions without bleak/cryptography installed."""
import sys, types, struct, importlib.util

# --- stub heavy imports so `import bitchat` succeeds in a bare sandbox ---
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

class _Dummy:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return lambda *a, **k: None

_mod("bleak", BleakClient=_Dummy, BleakScanner=_Dummy, BleakGATTCharacteristic=_Dummy)
_mod("bleak.backends")
_mod("bleak.backends.device", BLEDevice=_Dummy)
_mod("aioconsole", ainput=lambda *a, **k: None)
_mod("pybloom_live", BloomFilter=_Dummy)
_mod("encryption", EncryptionService=_Dummy, NoiseError=type("NoiseError", (Exception,), {}))
_mod("bitchat_compression", compress_if_beneficial=lambda b: (b, False), decompress=lambda b: b)
_mod("fragmentation", Fragment=_Dummy, FragmentType=_Dummy, fragment_payload=lambda *a, **k: [])
_mod("terminal_ux", ChatContext=_Dummy, ChatMode=_Dummy, Public=_Dummy, Channel=_Dummy,
     PrivateDM=_Dummy, format_message_display=lambda *a, **k: "", print_help=lambda *a, **k: None,
     clear_screen=lambda *a, **k: None)
_mod("persistence", AppState=_Dummy, load_state=lambda *a, **k: _Dummy(),
     save_state=lambda *a, **k: None, encrypt_password=lambda *a, **k: b"",
     decrypt_password=lambda *a, **k: "")

import os
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bitchat-python", "bitchat", "bitchat.py")
spec = importlib.util.spec_from_file_location("bitchat", PATH)
m = importlib.util.module_from_spec(spec)
sys.modules["bitchat"] = m   # dataclass in py3.10 needs the module registered
spec.loader.exec_module(m)

ok = 0
def check(label, cond, *info):
    global ok
    assert cond, f"FAIL: {label}  {info}"
    ok += 1
    print(f"  [pass] {label}")

# Reference iOS-style packet builder (independent of our encoder) ----------
def build_ref(sender_hex, ttl, mtype, payload, ts=1_719_200_000_000, recipient=None):
    b = bytearray()
    b.append(1)              # version
    b.append(mtype)         # type
    b.append(ttl)           # ttl  (offset 2)
    b += struct.pack(">Q", ts)   # timestamp (offset 3..10)
    flags = 0x01 if recipient is not None else 0
    b.append(flags)         # flags (offset 11)
    b += struct.pack(">H", len(payload))  # payload_len (offset 12..13)
    sb = bytes.fromhex(sender_hex); b += sb[:8] + bytes(8 - len(sb))  # sender (14..21)
    if recipient is not None:
        b += recipient
    b += payload
    return bytes(b)

print("A. parse a spec-built iOS ANNOUNCE packet")
ref = build_ref("aabbccddeeff0011", 5, int(m.MessageType.ANNOUNCE), b"Phone1", recipient=m.BROADCAST_RECIPIENT)
p = m.parse_bitchat_packet(ref)
check("sender_id read from offset 14", p.sender_id_str == "aabbccddeeff0011", p.sender_id_str)
check("ttl read from offset 2", p.ttl == 5, p.ttl)
check("payload (nickname) clean", p.payload == b"Phone1", p.payload)
check("recipient parsed as broadcast", p.recipient_id == m.BROADCAST_RECIPIENT)

print("B. THE BUG: old layout read sender from offset 2 (timestamp bytes)")
t1 = build_ref("aabbccddeeff0011", 5, 1, b"Phone1", ts=1_719_200_000_000)
t2 = build_ref("aabbccddeeff0011", 5, 1, b"Phone1", ts=1_719_200_005_000)  # 5s later
old1, old2 = t1[2:10].hex(), t2[2:10].hex()       # what the OLD parser used as sender
new1 = m.parse_bitchat_packet(t1).sender_id_str
new2 = m.parse_bitchat_packet(t2).sender_id_str
check("old layout: same phone -> DIFFERENT ids (the explosion)", old1 != old2, old1, old2)
check("new layout: same phone -> SAME stable id", new1 == new2 == "aabbccddeeff0011", new1, new2)

print("C. round-trip with our own encoder (broadcast)")
snd = "1122334455667788"
pkt = m.create_bitchat_packet(snd, m.MessageType.ANNOUNCE, b"Sage")
check("byte0 version=1", pkt[0] == 1)
check("byte2 ttl=7", pkt[2] == 7, pkt[2])
check("sender at offset 14", pkt[14:22].rstrip(b"\x00").hex() == snd, pkt[14:22].hex())
rp = m.parse_bitchat_packet(pkt)
check("round-trip sender", rp.sender_id_str == snd)
check("round-trip payload", rp.payload == b"Sage")
check("padded to >=256 (traffic shaping)", len(pkt) >= 256, len(pkt))

print("D. message payload round-trip (iOS BitchatMessage layout)")
mid = "550e8400-e29b-41d4-a716-446655440000"
mp = m.encode_message_payload(mid, "Sage", "hello world", 1_719_200_000_000,
                              sender_peer_id="1122334455667788")
msg = m.parse_bitchat_message_payload(mp)
check("content survives", msg.content == "hello world", msg.content)
check("id survives", msg.id == mid, msg.id)

print("E. full public MESSAGE packet end-to-end")
pl = m.encode_message_payload(mid, "Sage", "hi there", 1_719_200_000_000, sender_peer_id=snd)
fpkt = m.create_bitchat_packet(snd, m.MessageType.MESSAGE, pl)
fp = m.parse_bitchat_packet(fpkt)
check("type=MESSAGE", fp.msg_type == m.MessageType.MESSAGE)
fm = m.parse_bitchat_message_payload(fp.payload)
check("end-to-end content", fm.content == "hi there", fm.content)

print("F. clean_nickname strips binary suffix")
check("nul+binary trimmed", m.clean_nickname(b"Sage\x00\x13\x37\xff") == "Sage", m.clean_nickname(b"Sage\x00\x13\x37\xff"))
check("plain passes through", m.clean_nickname(b"Alice") == "Alice")

print("G. display_peers() dedups rotating ids by fingerprint")
class P: pass
d = P()
d.peers = {
    "aaaa": m.Peer(nickname="Phone1", fingerprint="fp1"),
    "bbbb": m.Peer(nickname="Phone1", fingerprint="fp1"),   # rotated id, same fp
    "cccc": m.Peer(nickname="Phone2", fingerprint="fp2"),
    "dddd": m.Peer(nickname="Phone3", fingerprint=None),    # no fp -> by nickname
}
names = m.BitchatClient.display_peers(d)
check("3 unique peers (not 4)", len(names) == 3, names)

print("H. parse_announce: REAL captured TLV bytes -> clean name + fingerprint")
h1 = "010943727970746f466f7802207b686bfd1c9a95c27d1d0fa85c60d4d6fbcc4609711f516b9ce825c06c134c5b0320dfeec34faf58451df1aaf3c96eba6ba4a43ae5ddcaac311d2f593918e23f7fcb04081c569069c07e2242"
h2 = "010a43727970746f466f783202202d42d7eb4db9fa6a71f7cb1f4dfdaa89001bae0e93b95b8471c2fc946e4a346103207292cb0c98a878b9ece74e3e0109317d74047a1266b127e7266631d6cd73716f0408b5990117c50ce9c0"
a1 = m.parse_announce(bytes.fromhex(h1))
a2 = m.parse_announce(bytes.fromhex(h2))
check("real announce #1 -> CryptoFox", a1["nickname"] == "CryptoFox", a1["nickname"])
check("real announce #2 -> CryptoFox2", a2["nickname"] == "CryptoFox2", a2["nickname"])
check("fingerprint from 0x02 noise key", isinstance(a1["fingerprint"], str) and len(a1["fingerprint"]) == 64, a1["fingerprint"])
check("bare-nickname fallback", m.parse_announce(b"CryptoFox")["nickname"] == "CryptoFox")

print("I. Sage's OUTGOING announce is valid TLV (phones can parse it)")
class _ES:
    def get_public_key(self): return bytes(range(32))
    def get_signing_public_key_bytes(self): return bytes(range(32, 64))
class _FC:
    nickname = "Sage"
    my_peer_id = "9e940b5e00000000"
    encryption_service = _ES()
_out = m.BitchatClient._build_announce_payload(_FC())
_pa = m.parse_announce(_out)
check("self-announce nickname round-trips", _pa["nickname"] == "Sage", _pa["nickname"])
check("self-announce carries fingerprint", isinstance(_pa["fingerprint"], str) and len(_pa["fingerprint"]) == 64)
check("self-announce begins with 0x01 nick TLV", _out[0]==0x01 and _out[1]==4 and _out[2:6]==b"Sage", _out[:6].hex())

print(f"\nALL {ok} CHECKS PASSED")
# --- end of file sentinel ---
