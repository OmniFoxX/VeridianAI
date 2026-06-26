# BitChat wire-format fix — 2026-06-25

## Root cause (not what we thought)
The peer-list explosion, garbled nicknames, "garbled binary" on Sage's side, and
phones-can't-see-Sage all trace to ONE bug: `bitchat.py` used an obsolete packet
header that does not match current BitChat (protocol whitepaper v1.1 / iOS source).

Real v1 header (14 bytes before sender):
  version(1) type(1) ttl(1) timestamp(8) flags(1) payload_len(2) sender_id(8) ...
Old local header:
  version(1) type(1) sender_id(8) flags(1) ttl(1) payload_len(2) ...   <-- WRONG

Because the old parser read sender_id from bytes 2-9, it was actually reading the
TTL byte + the first 7 bytes of the millisecond TIMESTAMP. The timestamp changes
on every packet, so every packet looked like a brand-new peer -> "+3 new IDs every
scan." Same offset error spilled binary into nicknames and made Sage's outbound
packets unparseable to the phones. Leo's "rotating BLE peer ID" was a symptom read.

## What changed
bitchat-python/bitchat/bitchat.py
  - parse_bitchat_packet / create_bitchat_packet(_with_recipient/_with_signature):
    rewritten to the real v1 layout (ttl@2, timestamp@3, flags@11, sender@14),
    broadcast recipient on non-fragment packets, + iOS PKCS#7 block padding
    (256/512/1024/2048). Ported from upstream kaganisildak/bitchat-python HEAD
    ("change for work with new ios version"), which your local copy had regressed.
  - encode_message_payload / parse_bitchat_message_payload: corrected to the iOS
    BitchatMessage layout (flags, timestamp, id-string, sender, content, peerID, channel).
  - TTL is written at byte 2 now (was byte 11) in 13 handshake/relay/ack sites.
  - clean_nickname(): cuts at first NUL, strips control bytes, trims.
  - Peer.fingerprint + dedup: identity announces map peer_id -> SHA256(static pubkey);
    rotated IDs (previousPeerID) are merged; display_peers() returns a de-duplicated
    list. So even if a phone rotates its ephemeral ID, it stays one entry.
  - send_private_message: encrypts the ENTIRE inner MESSAGE packet (iOS shape).

backend/bitchat_ble_gateway.py
  - /api/info, WS "peers", and the peer monitor now use display_peers() (deduped).
  - NEW POST /shutdown: leaves the mesh, drops BLE, stops the scanner, exits.

backend/tier_lifecycle.py
  - NEW stop_bitchat_gateway(): graceful POST /shutdown, then guarantees the
    process on :8080 is gone and the port is free.

backend/main.py
  - /api/socials/connect: for bitchat, "Connect" ensures the gateway is running;
    "Disconnect" now calls stop_bitchat_gateway() so OFF actually stops scanning.

## How to verify
1. Automated: `python backend/test_bitchat_wire.py`  (round-trip + reproduces the bug)
2. On your phones: launch, open the Socials tab, Connect BitChat.
   - Peer list should settle at 3 and show clean names (not growing every scan).
   - Phones should now see "Sage" appear.
   - Click Disconnect -> scanning stops (verify the python gateway process exits).

## Still open (needs a live capture to finish)
- Private DMs depend on the Noise-XX layer (encryption.py) being byte-compatible
  with Apple/Android. Framing + handshake initiation are fixed, but end-to-end DM
  decryption isn't verified offline.
- DM-by-username from the OracleAI UI isn't wired to the gateway's "DM:" format;
  public/channel messaging is the path that should work first.
- BitChat is now OPT-IN: it does NOT scan at boot. The gateway starts only when
  you click Connect, and the OFF button stops it. To make it auto-start on launch,
  set "bitchat_autostart": true in config.json (default is off).

## Safety
Pristine pre-change backup: archives/bitchat_backup_20260625_050407/

## Update 2026-06-25 (confirmed from live capture)
ANNOUNCE (type 0x01) payload is TLV: repeated [type:1][len:1][value:len].
  0x01 = nickname (UTF-8)
  0x02 = Noise static public key (32B)  -> fingerprint = SHA256(value)
  0x03 = signing public key (32B)
  0x04 = peer id (8B)
Verified against two captured packets (CryptoFox / CryptoFox2). parse_announce()
now extracts the nickname from 0x01 and the fingerprint from 0x02 (so dedup works
straight from the announce). The [ANNOUNCE-RAW] debug has been removed.
Kept: gateway "[GW] RECV from <name>: <msg>" log to confirm inbound delivery.

STILL TO CONFIRM: inbound delivery. Last run's log showed only Sage->BLE
(outbound). Send a PLAIN message from a phone and watch for "[GW] RECV".
