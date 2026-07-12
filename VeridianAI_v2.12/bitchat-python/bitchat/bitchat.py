#!/usr/bin/env python3
"""
bitchat.py — OracleAI BitChat BLE Client v2.11.11
Bluetooth Low Energy BitChat mesh client using bleak (Windows/WinRT).

Location/version agnostic: all imports are relative or stdlib.
No hardcoded drive letters or version strings.

Known bugs fixed vs previous version:
  - relay_data = packet.ttl - 1  (was: relay_data = packet.ttl - 1)
  - handle_channel_announce: list indexing parts/parts/etc
  - handle_join_channel: parts, parts indexing
  - handle_dm_command: parts strip
  - handle_block_command / handle_unblock_command: parts
  - handle_pass_command / handle_transfer_command: parts, parts
  - send_private_message: pending queue appends full tuple, not bare text
  - background_scanner(): implemented (was missing, called by gateway)
  - run(): removed references to self.target_address / self.on_disconnect
  - handshake_data = 3  (TTL byte, was: handshake_data = 3)
  - DeliveryTracker.track_message: accepts is_private kwarg
  - encode_message_payload / parse_bitchat_message_payload: implemented
  - create_bitchat_packet / create_bitchat_packet_with_recipient /
    create_bitchat_packet_with_signature: implemented
  - should_fragment, should_send_ack, unpad_message,
    derive_channel_key, compute_key_commitment: implemented
  - print_banner: implemented
  - JOIN MessageType: added (was missing from enum)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import struct
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import base64

from bleak import BleakClient, BleakScanner, BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
import aioconsole
from pybloom_live import BloomFilter

from encryption import EncryptionService, NoiseError
from bitchat_compression import compress_if_beneficial, decompress
from fragmentation import Fragment, FragmentType, fragment_payload
from terminal_ux import (
    ChatContext,
    ChatMode,
    Public,
    Channel,
    PrivateDM,
    format_message_display,
    print_help,
    clear_screen,
)
from persistence import (
    AppState,
    load_state,
    save_state,
    encrypt_password,
    decrypt_password,
)

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
VERSION = "v1.1.0"

# ---------------------------------------------------------------------------
# BLE UUIDs
# ---------------------------------------------------------------------------
BITCHAT_SERVICE_UUID        = "f47b5e2d-4a9e-4c5a-9b3f-8e1d2c3a4b5c"
BITCHAT_CHARACTERISTIC_UUID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
COVER_TRAFFIC_PREFIX = "☂DUMMY☂"

# Packet header flags
FLAG_HAS_RECIPIENT  = 0x01
FLAG_HAS_SIGNATURE  = 0x02
FLAG_IS_COMPRESSED  = 0x04

# Message payload flags
MSG_FLAG_IS_RELAY               = 0x01
MSG_FLAG_IS_PRIVATE             = 0x02
MSG_FLAG_HAS_ORIGINAL_SENDER    = 0x04
MSG_FLAG_HAS_RECIPIENT_NICKNAME = 0x08
MSG_FLAG_HAS_SENDER_PEER_ID     = 0x10
MSG_FLAG_HAS_MENTIONS           = 0x20
MSG_FLAG_HAS_CHANNEL            = 0x40
MSG_FLAG_IS_ENCRYPTED           = 0x80

SIGNATURE_SIZE      = 64
BROADCAST_RECIPIENT = b'\xFF' * 8

# Packet wire format
# [version:1] [type:1] [sender_id:8] [flags:1] [ttl:1] [payload_len:2] [payload:N]
# Optional after sender_id if FLAG_HAS_RECIPIENT: [recipient_id:8]
# Optional after payload if FLAG_HAS_SIGNATURE:   [signature:64]
PACKET_VERSION      = 0x01
HEADER_BASE_SIZE    = 14   # version(1)+type(1)+sender(8)+flags(1)+ttl(1)+payload_len(2)
MAX_PACKET_SIZE     = 512
FRAGMENT_THRESHOLD  = 480  # fragment if payload makes packet exceed this


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
class DebugLevel(IntEnum):
    CLEAN = 0
    BASIC = 1
    FULL  = 2

DEBUG_LEVEL = DebugLevel.CLEAN

def debug_println(*args, **kwargs):
    if DEBUG_LEVEL >= DebugLevel.BASIC:
        try:
            print(*args, **kwargs)
        except BlockingIOError:
            pass

def debug_full_println(*args, **kwargs):
    if DEBUG_LEVEL >= DebugLevel.FULL:
        try:
            print(*args, **kwargs)
        except BlockingIOError:
            pass


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------
class MessageType(IntEnum):
    # --- current BitChat protocol (permissionlesstech) canonical wire values --
    ANNOUNCE                  = 0x01
    MESSAGE                   = 0x02   # public broadcast chat message
    LEAVE                     = 0x03
    NOISE_HANDSHAKE           = 0x10   # unified Noise XX handshake (all stages)
    NOISE_ENCRYPTED           = 0x11   # transport-encrypted container
    FRAGMENT                  = 0x20   # unified fragment
    REQUEST_SYNC              = 0x21   # gossip state sync (ignored)
    FILE_TRANSFER             = 0x22
    # --- obsolete types, kept off the live wire but referenced by legacy code -
    KEY_EXCHANGE              = 0x0E
    CHANNEL_ANNOUNCE          = 0x08
    CHANNEL_RETENTION         = 0x09
    DELIVERY_ACK              = 0x0A
    DELIVERY_STATUS_REQUEST   = 0x0B
    READ_RECEIPT              = 0x0C
    NOISE_IDENTITY_ANNOUNCE   = 0x13
    CHANNEL_KEY_VERIFY_REQUEST  = 0x14
    CHANNEL_KEY_VERIFY_RESPONSE = 0x15
    CHANNEL_PASSWORD_UPDATE   = 0x16
    CHANNEL_METADATA          = 0x17
    VERSION_HELLO             = 0x2A
    VERSION_ACK               = 0x2B
    JOIN                      = 0x2C
    # --- back-compat aliases (resolve to the canonical members above) ---------
    NOISE_HANDSHAKE_INIT      = 0x10   # -> NOISE_HANDSHAKE
    NOISE_HANDSHAKE_RESP      = 0x10   # -> NOISE_HANDSHAKE
    FRAGMENT_START            = 0x20   # -> FRAGMENT
    FRAGMENT_CONTINUE         = 0x20   # -> FRAGMENT
    FRAGMENT_END              = 0x20   # -> FRAGMENT


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Peer:
    nickname: Optional[str] = None
    fingerprint: Optional[str] = None


@dataclass
class BitchatPacket:
    msg_type:         MessageType
    sender_id:        bytes
    sender_id_str:    str
    recipient_id:     Optional[bytes]
    recipient_id_str: Optional[str]
    payload:          bytes
    ttl:              int


@dataclass
class BitchatMessage:
    id:                str
    content:           str
    channel:           Optional[str]
    is_encrypted:      bool
    encrypted_content: Optional[bytes]


@dataclass
class DeliveryAck:
    original_message_id: str
    ack_id:              str
    recipient_id:        str
    recipient_nickname:  str
    timestamp:           int
    hop_count:           int


# ---------------------------------------------------------------------------
# Packet builder helpers
# ---------------------------------------------------------------------------
def create_bitchat_packet(
    sender_id: str,
    msg_type: MessageType,
    payload: bytes,
    ttl: int = 7,
) -> bytes:
    """Create a broadcast BitChat packet (iOS/Android-compatible)."""
    return create_bitchat_packet_with_recipient(
        sender_id, None, msg_type, payload, None, ttl
    )


def create_bitchat_packet_with_recipient(
    sender_id: str,
    recipient_id: Optional[str],
    msg_type: MessageType,
    payload: bytes,
    signature: Optional[bytes] = None,
    ttl: int = 7,
) -> bytes:
    """Create a BitChat packet in the iOS/Android v1 wire format, padded to a
    standard block size for traffic-analysis resistance.

    Layout: version·type·ttl·timestamp(8)·flags·payload_len(2)·sender(8)
            ·[recipient(8)]·payload·[signature(64)]·[PKCS#7 padding]."""
    packet = bytearray()
    packet.append(1)                                  # version
    packet.append(int(msg_type))                      # type
    packet.append(ttl & 0xFF)                         # ttl
    packet.extend(struct.pack('>Q', int(time.time() * 1000)))  # timestamp (ms)

    flags = 0
    exclude_recipient_types = (
        MessageType.FRAGMENT_START,
        MessageType.FRAGMENT_CONTINUE,
        MessageType.FRAGMENT_END,
    )
    # iOS sets a (broadcast) recipient on everything except fragments.
    if recipient_id is not None or msg_type not in exclude_recipient_types:
        flags |= FLAG_HAS_RECIPIENT
    if signature:
        flags |= FLAG_HAS_SIGNATURE
    packet.append(flags)

    packet.extend(struct.pack('>H', len(payload)))    # payload length

    sender_bytes = bytes.fromhex(sender_id)
    packet.extend(sender_bytes[:8])
    if len(sender_bytes) < 8:
        packet.extend(bytes(8 - len(sender_bytes)))

    if flags & FLAG_HAS_RECIPIENT:
        if recipient_id:
            recipient_bytes = bytes.fromhex(recipient_id)
            packet.extend(recipient_bytes[:8])
            if len(recipient_bytes) < 8:
                packet.extend(bytes(8 - len(recipient_bytes)))
        else:
            packet.extend(BROADCAST_RECIPIENT)

    packet.extend(payload)

    if signature:
        packet.extend(signature[:SIGNATURE_SIZE])

    # iOS-style PKCS#7 padding up to the next standard block size.
    block_sizes = (256, 512, 1024, 2048)
    total_size  = len(packet) + 16          # leave room for AEAD tag overhead
    target_size = None
    for bs in block_sizes:
        if total_size <= bs:
            target_size = bs
            break
    if target_size is not None:
        padding_needed = target_size - len(packet)
        if 0 < padding_needed <= 255:
            pad = bytearray(os.urandom(padding_needed - 1))
            pad.append(padding_needed)
            packet.extend(pad)

    return bytes(packet)


def create_bitchat_packet_with_signature(
    sender_id: str,
    msg_type: MessageType,
    payload: bytes,
    signature: Optional[bytes],
    ttl: int = 7,
) -> bytes:
    """Create a broadcast BitChat packet carrying an Ed25519 signature."""
    return create_bitchat_packet_with_recipient(
        sender_id, None, msg_type, payload, signature, ttl
    )


def _optimal_block_size(data_len: int) -> int:
    """Match BitChat MessagePadding.optimalBlockSize: +16 AEAD overhead, then
    the smallest of 256/512/1024/2048 that fits; otherwise the raw size."""
    total = data_len + 16
    for bs in (256, 512, 1024, 2048):
        if total <= bs:
            return bs
    return data_len


def _pkcs7_pad_to_block(data: bytes) -> bytes:
    """PKCS#7 pad to the optimal block size, all pad bytes == pad length
    (matches BitChat MessagePadding.pad). No-op if it cannot fit in <=255 pad."""
    target = _optimal_block_size(len(data))
    if len(data) >= target:
        return data
    need = target - len(data)
    if need <= 0 or need > 255:
        return data
    return bytes(data) + bytes([need]) * need


def create_signed_bitchat_packet(
    sender_id: str,
    msg_type: MessageType,
    payload: bytes,
    sign_func,
    ttl: int = 7,
    recipient_id: Optional[str] = None,
) -> bytes:
    """Build an Ed25519-signed BitChat packet matching current (permissionlesstech)
    BitChat, which REQUIRES a valid signature to trust/display a peer.

    The signature covers `toBinaryDataForSigning()`: the UNPADDED packet encoded
    with ttl=0 and no signature (and no signature flag). The transmitted packet
    then carries the real ttl, the HAS_SIGNATURE flag, and the 64-byte signature
    appended after the payload. No block padding (current BitChat sends unpadded)."""
    timestamp = int(time.time() * 1000)
    sb = bytes.fromhex(sender_id)[:8]
    sb = sb + bytes(8 - len(sb))
    rb = None
    if recipient_id is not None:
        rb = bytes.fromhex(recipient_id)[:8]
        rb = rb + bytes(8 - len(rb))

    def _encode(ttl_val: int, flags_val: int, sig: Optional[bytes]) -> bytes:
        pkt = bytearray()
        pkt.append(1)                                   # version
        pkt.append(int(msg_type))                       # type
        pkt.append(ttl_val & 0xFF)                      # ttl
        pkt.extend(struct.pack('>Q', timestamp))        # timestamp (ms)
        pkt.append(flags_val)                           # flags
        pkt.extend(struct.pack('>H', len(payload)))     # payload length
        pkt.extend(sb)                                  # sender (8)
        if flags_val & FLAG_HAS_RECIPIENT:
            pkt.extend(rb if rb is not None else BROADCAST_RECIPIENT)
        pkt.extend(payload)
        if sig:
            pkt.extend(sig[:SIGNATURE_SIZE])
        return bytes(pkt)

    flags_base = FLAG_HAS_RECIPIENT if recipient_id is not None else 0
    # Current BitChat signs the PADDED, unsigned (ttl=0) representation and then
    # transmits the PADDED signed packet. Padding is PKCS#7 to the optimal block
    # size with every pad byte equal to the pad length.
    signing_repr = _pkcs7_pad_to_block(_encode(0, flags_base, None))
    signature = sign_func(signing_repr)
    signed = _encode(ttl, flags_base | FLAG_HAS_SIGNATURE, signature)
    return _pkcs7_pad_to_block(signed)


# ---------------------------------------------------------------------------
# Packet parser
# ---------------------------------------------------------------------------
def clean_nickname(raw) -> str:
    """Extract a human-readable nickname, dropping any binary metadata suffix.

    A correct BitChat ANNOUNCE payload is just the UTF-8 nickname, but a padded
    or mis-framed packet can leave trailing NULs / control bytes glued on. Cut
    at the first NUL, drop control characters, trim, and cap the length."""
    if isinstance(raw, (bytes, bytearray)):
        nul = raw.find(b'\x00')
        if nul != -1:
            raw = raw[:nul]
        text = bytes(raw).decode('utf-8', errors='ignore')
    else:
        text = str(raw)
    text = ''.join(c for c in text if c == ' ' or (0x20 <= ord(c) and ord(c) != 0x7f))
    return text.strip()[:64]


def parse_announce(payload: bytes) -> dict:
    """Parse a BitChat ANNOUNCE payload, which is TLV-encoded:
    a sequence of [type:1][len:1][value:len] fields.

        0x01 = nickname (UTF-8)
        0x02 = Noise static public key  -> fingerprint = SHA256(key)
        0x03 = signing public key
        0x04 = peer id

    Returns {"nickname", "fingerprint"}. Falls back to a bare-nickname decode
    for older / non-TLV builds (where no 0x01 field is found)."""
    out = {"nickname": None, "fingerprint": None}
    i, n = 0, len(payload)
    while i + 2 <= n:
        t = payload[i]
        ln = payload[i + 1]
        if i + 2 + ln > n:
            break
        val = payload[i + 2:i + 2 + ln]
        if t == 0x01:
            out["nickname"] = clean_nickname(val)
        elif t == 0x02 and ln:
            out["fingerprint"] = hashlib.sha256(bytes(val)).hexdigest()
        i += 2 + ln
    if not out["nickname"]:
        out["nickname"] = clean_nickname(payload)
    return out


def _wire_id_str(b8: bytes) -> str:
    """8-byte wire ID -> stable 16-hex string.

    v2.12.5 FIX: this used to be b8.rstrip(b'\x00').hex(), which CORRUPTS any
    modern binary peer ID whose last byte(s) happen to be 0x00 (~3%% of IDs
    have at least one trailing zero byte). The trimmed hex then fails the
    `recipient_id_str == my_peer_id` checks, so handshakes and DMs for us get
    silently ignored -- the "handshake stuck at pending" heisenbug. Peer IDs
    are FIXED 8 bytes in the current protocol; never trim them.

    Narrow legacy path kept: very old builds sent the ID as NUL-padded ASCII
    hex text; when the field decodes as short printable ASCII hex, keep the
    old behavior so those peers stay stable."""
    stripped = b8.rstrip(b'\x00')
    if 0 < len(stripped) < 8:
        try:
            txt = stripped.decode('ascii')
            if all(c in '0123456789abcdefABCDEF' for c in txt):
                return stripped.hex()
        except (UnicodeDecodeError, ValueError):
            pass
    return b8.hex()


def parse_bitchat_packet(data: bytes) -> BitchatPacket:
    """Parse a BitChat packet (iOS/Android-compatible v1 wire format).

    Wire layout:
        version(1) type(1) ttl(1) timestamp(8) flags(1) payload_len(2)
        sender_id(8) [recipient_id(8) if HAS_RECIPIENT]
        payload(payload_len) [signature(64) if HAS_SIGNATURE]
        [PKCS#7 block padding to 256/512/1024/2048 -- ignored via payload_len]
    """
    HEADER_SIZE       = 13
    SENDER_ID_SIZE    = 8
    RECIPIENT_ID_SIZE = 8

    if len(data) < HEADER_SIZE + SENDER_ID_SIZE:
        raise ValueError(f"Packet too small: {len(data)} bytes")

    offset = 0

    version = data[offset]; offset += 1
    if version not in (1, 2):
        raise ValueError(f"Unsupported version: {version}")

    msg_type_b = data[offset]; offset += 1
    ttl        = data[offset]; offset += 1
    offset    += 8  # skip 8-byte timestamp

    flags = data[offset]; offset += 1
    has_recipient = (flags & FLAG_HAS_RECIPIENT) != 0
    has_signature = (flags & FLAG_HAS_SIGNATURE) != 0
    is_compressed = (flags & FLAG_IS_COMPRESSED) != 0
    has_route     = (version == 2) and (flags & 0x08) != 0   # v2 source route

    # v2 uses a 4-byte payload length; v1 uses 2 bytes.
    if version == 2:
        payload_len = struct.unpack('>I', data[offset:offset + 4])[0]; offset += 4
    else:
        payload_len = struct.unpack('>H', data[offset:offset + 2])[0]; offset += 2

    # Sender ID -- FIXED 8 bytes (v2.12.5: no NUL trim; see _wire_id_str)
    sender_id     = data[offset:offset + SENDER_ID_SIZE]
    sender_id_str = _wire_id_str(sender_id)
    offset += SENDER_ID_SIZE

    recipient_id = None
    recipient_id_str = None
    if has_recipient:
        recipient_id     = data[offset:offset + RECIPIENT_ID_SIZE]
        recipient_id_str = _wire_id_str(recipient_id)
        offset += RECIPIENT_ID_SIZE

    # v2 source route: [hopCount:1][hopCount x 8-byte peer IDs]. Sage is the
    # endpoint, so we skip it. Route bytes are NOT counted in payload_len.
    if has_route and offset < len(data):
        route_count = data[offset]; offset += 1
        offset += route_count * SENDER_ID_SIZE

    payload_end = offset + payload_len
    if len(data) < payload_end:
        raise ValueError(
            f"Packet truncated: expected {payload_end} bytes, got {len(data)}"
        )
    payload = data[offset:payload_end]
    offset  = payload_end

    signature = None
    if has_signature and len(data) >= offset + SIGNATURE_SIZE:
        signature = data[offset:offset + SIGNATURE_SIZE]

    if is_compressed:
        # Compressed payload framing: [originalSize:(2 v1 / 4 v2) BE][raw-DEFLATE].
        try:
            _pfx = 4 if version == 2 else 2
            if len(payload) >= _pfx:
                _orig = int.from_bytes(bytes(payload[:_pfx]), 'big')
                _dec  = decompress(bytes(payload[_pfx:]))
                if (not _orig) or len(_dec) == _orig:
                    payload = _dec
        except Exception:
            pass

    if isinstance(payload, bytearray):
        payload = bytes(payload)

    try:
        msg_type = MessageType(msg_type_b)
    except ValueError:
        msg_type = MessageType.MESSAGE  # treat unknown as MESSAGE

    return BitchatPacket(
        msg_type         = msg_type,
        sender_id        = sender_id,
        sender_id_str    = sender_id_str,
        recipient_id     = recipient_id,
        recipient_id_str = recipient_id_str,
        payload          = payload,
        ttl              = ttl,
    )


# ---------------------------------------------------------------------------
# Message payload encode / decode
# ---------------------------------------------------------------------------
def encode_message_payload(
    msg_id:         str,
    sender_nick:    str,
    content:        str,
    timestamp_ms:   int,
    channel:        Optional[str] = None,
    channel_key:    Optional[bytes] = None,
    recipient_id:   Optional[str] = None,
    sender_peer_id: Optional[str] = None,
) -> bytes:
    """Encode a BitchatMessage payload in the iOS/Android-compatible layout:

        flags(1) · timestamp(8) · id(1+len) · sender(1+len) · content(2+len)
        · [senderPeerID(1+len)] · [channel(1+len)]
    """
    is_private   = recipient_id is not None
    is_encrypted = bool(channel and channel_key)

    encrypted_content = None
    if is_encrypted:
        try:
            _enc = EncryptionService()
            encrypted_content = _enc.encrypt_for_channel(
                content, channel, channel_key, ''
            )
        except Exception:
            is_encrypted = False
            encrypted_content = None

    data = bytearray()

    flags = 0
    if is_private:     flags |= MSG_FLAG_IS_PRIVATE
    if sender_peer_id: flags |= MSG_FLAG_HAS_SENDER_PEER_ID
    if channel:        flags |= MSG_FLAG_HAS_CHANNEL
    if is_encrypted:   flags |= MSG_FLAG_IS_ENCRYPTED
    data.append(flags)

    data.extend(struct.pack('>Q', int(timestamp_ms)))

    id_bytes = str(msg_id).encode('utf-8')[:255]
    data.append(len(id_bytes)); data.extend(id_bytes)

    nick_bytes = sender_nick.encode('utf-8')[:255]
    data.append(len(nick_bytes)); data.extend(nick_bytes)

    body = encrypted_content if (is_encrypted and encrypted_content) \
        else content.encode('utf-8')
    data.extend(struct.pack('>H', len(body))); data.extend(body)

    if sender_peer_id:
        pid_bytes = str(sender_peer_id).encode('utf-8')[:255]
        data.append(len(pid_bytes)); data.extend(pid_bytes)

    if channel:
        ch_bytes = channel.encode('utf-8')[:255]
        data.append(len(ch_bytes)); data.extend(ch_bytes)

    return bytes(data)


def parse_bitchat_message_payload(data: bytes) -> BitchatMessage:
    """Parse a BitchatMessage payload (iOS/Android-compatible layout)."""
    offset = 0

    flags = data[offset]; offset += 1
    is_private         = (flags & MSG_FLAG_IS_PRIVATE) != 0
    has_sender_peer_id = (flags & MSG_FLAG_HAS_SENDER_PEER_ID) != 0
    has_channel        = (flags & MSG_FLAG_HAS_CHANNEL) != 0
    is_encrypted       = (flags & MSG_FLAG_IS_ENCRYPTED) != 0

    offset += 8  # skip 8-byte timestamp

    id_len = data[offset]; offset += 1
    id_str = data[offset:offset + id_len].decode('utf-8', errors='ignore')
    offset += id_len

    sender_len = data[offset]; offset += 1
    _sender = data[offset:offset + sender_len].decode('utf-8', errors='ignore')
    offset += sender_len

    content_len = struct.unpack('>H', data[offset:offset + 2])[0]; offset += 2
    content_bytes = data[offset:offset + content_len]; offset += content_len

    content = ""
    encrypted_content = None
    if is_encrypted:
        encrypted_content = content_bytes
    else:
        content = content_bytes.decode('utf-8', errors='ignore')

    if has_sender_peer_id and offset < len(data):
        peer_id_len = data[offset]; offset += 1
        offset += peer_id_len  # skip senderPeerID

    channel = None
    if has_channel and offset < len(data):
        channel_len = data[offset]; offset += 1
        channel = data[offset:offset + channel_len].decode('utf-8', errors='ignore')

    return BitchatMessage(id_str, content, channel, is_encrypted, encrypted_content)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def should_fragment(packet: bytes) -> bool:
    """Return True if the packet exceeds the BLE fragment threshold."""
    return len(packet) > FRAGMENT_THRESHOLD


def _unpadded_len(packet: bytes) -> int:
    """Real (pre-padding) length of a BitChat packet = header + sender +
    [recipient] + [route] + payload + [signature], excluding PKCS#7 padding.
    Sage sends v1; v2 handled defensively."""
    try:
        if len(packet) < 14:
            return len(packet)
        version = packet[0]
        flags   = packet[11]
        has_recipient = bool(flags & FLAG_HAS_RECIPIENT)
        has_signature = bool(flags & FLAG_HAS_SIGNATURE)
        has_route     = (version == 2) and bool(flags & 0x08)
        if version == 2:
            plen = int.from_bytes(packet[12:16], "big"); off = 16
        else:
            plen = (packet[12] << 8) | packet[13]; off = 14
        off += 8                      # sender
        if has_recipient:
            off += 8
        if has_route and off < len(packet):
            off += 1 + packet[off] * 8
        off += plen
        if has_signature:
            off += SIGNATURE_SIZE
        return min(off, len(packet))
    except Exception:
        return len(packet)


def unpad_message(data: bytes) -> bytes:
    """Strip PKCS#7-style zero padding from a decrypted message."""
    if not data:
        return data
    # If last byte looks like a pad length and all pad bytes match, strip them.
    pad_len = data[-1]
    if 1 <= pad_len <= 16 and len(data) >= pad_len:
        if all(b == pad_len for b in data[-pad_len:]):
            return data[:-pad_len]
    return data


def derive_channel_key(password: str, channel: str = "") -> bytes:
    """
    Derive a 32-byte channel encryption key from a password.
    Delegates to EncryptionService.derive_channel_key for consistency
    with the Swift implementation.
    """
    return EncryptionService.derive_channel_key(password, channel)


def compute_key_commitment(key: bytes) -> str:
    """SHA-256 hex digest of the channel key — used for key verification."""
    return hashlib.sha256(key).hexdigest()


def should_send_ack(
    is_private:  bool,
    channel:     Optional[str],
    recipient:   Optional[str],
    my_nickname: str,
    peer_count:  int,
) -> bool:
    """
    Decide whether to send a delivery ACK for a received message.
    ACKs are sent for private messages and small meshes (≤ 10 peers).
    """
    if is_private:
        return True
    if peer_count <= 10:
        return True
    return False


def print_banner():
    """Print the BitChat ASCII banner."""
    print("\033[38;5;46m")
    print("  ╔══════════════════════════════════════╗")
    print("  ║         BitChat  ·  OracleAI         ║")
    print(f" ║            {VERSION:<10}             ║")
    print("  ║   Bluetooth mesh chat — no internet  ║")
    print("  ╚══════════════════════════════════════╝")
    print("\033[0m")


# ---------------------------------------------------------------------------
# Delivery tracker
# ---------------------------------------------------------------------------
class DeliveryTracker:
    def __init__(self):
        # message_id -> (content, sent_time, is_private)
        self.pending_messages: Dict[str, Tuple[str, float, bool]] = {}
        self.sent_acks: Set[str] = set()

    def track_message(
        self,
        message_id: str,
        content:    str = "",
        is_private: bool = False,
    ):
        self.pending_messages[message_id] = (content, time.time(), is_private)

    def mark_delivered(self, message_id: str) -> bool:
        return self.pending_messages.pop(message_id, None) is not None

    def should_send_ack(self, ack_id: str) -> bool:
        if ack_id in self.sent_acks:
            return False
        self.sent_acks.add(ack_id)
        return True


# ---------------------------------------------------------------------------
# Fragment collector
# ---------------------------------------------------------------------------
class FragmentCollector:
    def __init__(self):
        # fragment_id_hex -> {index: data}
        self.fragments: Dict[str, Dict[int, bytes]] = {}
        # fragment_id_hex -> (total, original_type, sender_id_str)
        self.metadata:  Dict[str, Tuple[int, int, str]] = {}

    def add_fragment(
        self,
        fragment_id:   bytes,
        index:         int,
        total:         int,
        original_type: int,
        data:          bytes,
        sender_id:     str,
    ) -> Optional[Tuple[bytes, str]]:
        fid = fragment_id.hex()

        debug_full_println(
            f"[COLLECTOR] Adding fragment {index + 1}/{total} for ID {fid[:8]}"
        )

        if fid not in self.fragments:
            debug_full_println(
                f"[COLLECTOR] Creating new fragment collection for ID {fid[:8]}"
            )
            self.fragments[fid] = {}
            self.metadata[fid]  = (total, original_type, sender_id)

        self.fragments[fid] [index] = data

        debug_full_println(
            f"[COLLECTOR] Fragment {index + 1} stored. "
            f"Have {len(self.fragments[fid])}/{total} fragments"
        )

        if len(self.fragments[fid]) == total:
            debug_full_println("[COLLECTOR] ✓ All fragments received! Reassembling...")

            complete = bytearray()
            for i in range(total):
                if i in self.fragments[fid]:
                    complete.extend(self.fragments[fid] [i])
                else:
                    debug_full_println(f"[COLLECTOR] ✗ Missing fragment {i + 1}")
                    return None

            debug_full_println(
                f"[COLLECTOR] ✓ Reassembly complete: {len(complete)} bytes total"
            )

            sender = self.metadata.get(fid, (0, 0, "Unknown"))
            del self.fragments[fid]
            del self.metadata[fid]

            return (bytes(complete), sender)

        return None


# ---------------------------------------------------------------------------
# BitchatClient
# ---------------------------------------------------------------------------
class BitchatClient:
    def __init__(self):
        self.my_peer_id   = os.urandom(8).hex()  # provisional; derived from the
                                                 # Noise key below once it's loaded
        self.nickname     = "my-python-client"
        self.peers:        Dict[str, Peer]  = {}
        self.bloom         = BloomFilter(capacity=500, error_rate=0.01)
        self.processed_messages: Set[str]  = set()
        self.fragment_collector  = FragmentCollector()
        self.delivery_tracker    = DeliveryTracker()
        self.chat_context        = ChatContext()
        self.channel_keys:        Dict[str, bytes] = {}
        self.app_state            = AppState()
        self.blocked_peers:       Set[str]  = set()
        self.channel_creators:    Dict[str, str]   = {}
        self.password_protected_channels: Set[str] = set()
        self.channel_key_commitments:     Dict[str, str]   = {}
        self.discovered_channels: Set[str] = set()
        self.encryption_service   = EncryptionService()
        # BitChat derives the 8-byte peer ID from the Noise static public key:
        #   peerID == SHA256(noiseStaticPublicKey)[:8]  (16 hex chars).
        # iOS validates this binding (getCryptoIdentitiesByPeerIDPrefix) and will
        # NOT surface a peer whose ID doesn't match its key; Android is lenient.
        # Deriving it here also makes Sage's identity STABLE across launches,
        # since her Noise key is persisted.
        try:
            _noise_pub = self.encryption_service.get_public_key()
            if _noise_pub:
                self.my_peer_id = hashlib.sha256(bytes(_noise_pub)).hexdigest()[:16]
        except Exception as _e:
            debug_println(f"[IDENTITY] peer_id derivation failed, using random: {_e}")
        self.client:     Optional[BleakClient]           = None
        self.characteristic: Optional[BleakGATTCharacteristic] = None
        self.running     = True
        self.background_scanner_task: Optional[asyncio.Task] = None
        self.disconnection_callback_registered = False

        self.handshake_attempt_times: Dict[str, float] = {}
        self.handshake_timeout = 5.0

        # peer_id -> [(content, nickname, message_id), ...]
        self.pending_private_messages: Dict[str, List[Tuple[str, str, str]]] = {}

        self.encryption_service.on_peer_authenticated = self._on_peer_authenticated
        self.encryption_service.on_handshake_required = self._on_handshake_required

    # ------------------------------------------------------------------
    # Encryption callbacks
    # ------------------------------------------------------------------
    def _on_peer_authenticated(self, peer_id: str, fingerprint: str):
        debug_println(
            f"[NOISE] Peer {peer_id} authenticated with fingerprint: "
            f"{fingerprint[:16]}..."
        )
        asyncio.create_task(self.send_pending_private_messages(peer_id))

    def _on_handshake_required(self, peer_id: str):
        debug_println(f"[NOISE] Handshake required for peer {peer_id}")

    def display_peers(self):
        """De-duplicated peer list for the UI: collapse rotating peer IDs by
        stable fingerprint (falling back to nickname, then peer_id)."""
        seen = {}
        for pid, p in self.peers.items():
            nick = getattr(p, 'nickname', None)
            fp   = getattr(p, 'fingerprint', None)
            key  = fp or (nick.lower() if nick else None) or pid
            if key not in seen:
                seen[key] = nick or pid
        return sorted(seen.values(), key=lambda s: s.lower())

    def _build_announce_payload(self) -> bytes:
        """Build the TLV ANNOUNCE payload that current iOS/Android BitChat
        expects (and that this client already parses in parse_announce):

            [0x01][len] nickname  ·  [0x02][len] Noise static public key
            [0x03][len] signing public key  ·  [0x04][8] peer id

        Older builds accepted a bare nickname; current BitChat silently ignores
        an announce that isn't this TLV, which is why peers couldn't see us."""
        out = bytearray()
        nick = self.nickname.encode("utf-8")[:255]
        out += bytes([0x01, len(nick)]) + nick
        try:
            noise_pub = self.encryption_service.get_public_key()
            if noise_pub:
                noise_pub = bytes(noise_pub)
                out += bytes([0x02, len(noise_pub)]) + noise_pub
            sign_pub = self.encryption_service.get_signing_public_key_bytes()
            if sign_pub:
                sign_pub = bytes(sign_pub)
                out += bytes([0x03, len(sign_pub)]) + sign_pub
        except Exception as e:
            debug_println(f"[ANNOUNCE] key fetch failed, sending minimal announce: {e}")
        # TLV 0x04 = directNeighbors: the 8-byte peer IDs of peers directly
        # connected to us. Current BitChat only confirms a mesh edge (and shows
        # the peer / accepts its traffic) when BOTH sides list each other, so we
        # advertise the peers we're connected to here.
        try:
            neighbors = b""
            for pid in list(self.peers.keys())[:10]:
                pb = bytes.fromhex(pid)[:8]
                if len(pb) == 8:
                    neighbors += pb
            if neighbors and len(neighbors) % 8 == 0:
                out += bytes([0x04, len(neighbors)]) + neighbors
        except Exception:
            pass
        return bytes(out)

    # ------------------------------------------------------------------
    # Pending private message queue
    # ------------------------------------------------------------------
    async def send_pending_private_messages(self, peer_id: str):
        if peer_id not in self.pending_private_messages:
            return

        pending = self.pending_private_messages.pop(peer_id, [])
        if not pending:
            return

        debug_println(
            f"[NOISE] Sending {len(pending)} pending messages to {peer_id}"
        )

        for content, nickname, message_id in pending:
            try:
                await asyncio.sleep(0.3)
                await self.send_private_message(
                    content, peer_id, nickname, message_id
                )
                await asyncio.sleep(0.2)
            except Exception as e:
                debug_println(
                    f"[NOISE] Failed to send pending message to {peer_id}: {e}"
                )
                if "blocking" in str(e).lower():
                    debug_println(
                        f"[NOISE] Re-queuing message due to BLE congestion"
                    )
                    if peer_id not in self.pending_private_messages:
                        self.pending_private_messages[peer_id] = []
                    self.pending_private_messages[peer_id].append(
                        (content, nickname, message_id)
                    )
                    break

    # ------------------------------------------------------------------
    # BLE scan
    # ------------------------------------------------------------------
    async def find_device(self) -> Optional[BLEDevice]:
        debug_println(" Scanning for bitchat service...")

        devices = await BleakScanner.discover(
            timeout=5.0,
            service_uuids=[BITCHAT_SERVICE_UUID],
        )

        for device in devices:
            debug_full_println(
                f"Found device: {device.name} - {device.address}"
            )
            return device   # return first match

        return None

    # ------------------------------------------------------------------
    # Disconnect handler
    # ------------------------------------------------------------------
    def handle_disconnect(self, client: BleakClient):
        print(
            f"\r\033[K\033[91m✗ Disconnected from BitChat network\033[0m"
        )
        print("\033[90m» Scanning for other devices...\033[0m")
        print("> ", end='', flush=True)

        self.client         = None
        self.characteristic = None
        self.peers.clear()
        self.chat_context.active_dms.clear()

        self.encryption_service.sessions.clear()
        self.encryption_service.handshake_states.clear()

        self.pending_private_messages.clear()

        if isinstance(self.chat_context.current_mode, PrivateDM):
            self.chat_context.switch_to_public()

        if (
            not self.background_scanner_task
            or self.background_scanner_task.done()
        ):
            self.background_scanner_task = asyncio.create_task(
                self.background_scanner()
            )

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------
    async def connect(self) -> bool:
        print("\033[90m» Scanning for bitchat service...\033[0m")

        scan_attempts      = 0
        max_initial_attempts = 10
        device             = None

        while not device and self.running:
            device = await self.find_device()
            if not device:
                scan_attempts += 1
                if scan_attempts == max_initial_attempts:
                    print("\033[93m» No other BitChat devices found yet.\033[0m")
                    print("\033[90m» This might be because:\033[0m")
                    print("\033[90m  • You're the first one here (that's okay!)\033[0m")
                    print("\033[90m  • Other devices are out of Bluetooth range\033[0m")
                    print("\033[90m  • The iOS/Android app needs to be open\033[0m")
                    print("\033[90m» Continuing to scan in the background...\033[0m")
                    print("\033[90m» You can start using commands while waiting.\033[0m")
                    return True   # offline mode is valid
                await asyncio.sleep(1)

        if not self.running:
            return False

        print("\033[90m» Found bitchat service! Connecting...\033[0m")
        debug_println(" Match Found! Connecting...")

        try:
            self.client = BleakClient(
                device.address,
                disconnected_callback=self.handle_disconnect,
            )
            await self.client.connect()

            services = self.client.services
            if not services:
                raise Exception("No services found on device")

            for service in services:
                for char in service.characteristics:
                    if char.uuid.lower() == BITCHAT_CHARACTERISTIC_UUID.lower():
                        self.characteristic = char
                        debug_println(f" Found characteristic: {char.uuid}")
                        break
                if self.characteristic:
                    break

            if not self.characteristic:
                raise Exception("Characteristic not found")

            await self.client.start_notify(
                self.characteristic, self.notification_handler
            )

            debug_println(" Connection established.")
            return True

        except Exception as e:
            print(f"\n\033[91m❌ Connection failed\033[0m")
            print(f"\033[90mReason: {e}\033[0m")
            print("\033[90mPlease check:\033[0m")
            print("\033[90m  • Bluetooth is enabled\033[0m")
            print("\033[90m  • The other device is running BitChat\033[0m")
            print("\033[90m  • You're within range\033[0m")
            print("\n\033[90mTry running the command again.\033[0m")
            return False

    # ------------------------------------------------------------------
    # Background scanner — continuous reconnection loop
    # Called by gateway after initial connect()+handshake() sequence.
    # Also spawned by handle_disconnect() on unexpected drops.
    # ------------------------------------------------------------------
    async def background_scanner(self):
        debug_println("[BG] Background scanner started")

        while self.running:
            # If already connected, just sleep and check again
            if self.client and self.client.is_connected:
                await asyncio.sleep(5)
                continue

            debug_println("[BG] Not connected — scanning for peers...")

            try:
                device = await self.find_device()
                if device:
                    debug_println(
                        f"[BG] Found device: {device.address} — reconnecting..."
                    )
                    try:
                        self.client = BleakClient(
                            device.address,
                            disconnected_callback=self.handle_disconnect,
                        )
                        await self.client.connect()

                        # Re-discover characteristic
                        self.characteristic = None
                        for service in self.client.services:
                            for char in service.characteristics:
                                if (
                                    char.uuid.lower()
                                    == BITCHAT_CHARACTERISTIC_UUID.lower()
                                ):
                                    self.characteristic = char
                                    break
                            if self.characteristic:
                                break

                        if not self.characteristic:
                            raise Exception("Characteristic not found on reconnect")

                        await self.client.start_notify(
                            self.characteristic, self.notification_handler
                        )

                        print(
                            "\r\033[K\033[92m✓ Reconnected to BitChat network\033[0m"
                        )
                        print("> ", end='', flush=True)

                        # Re-announce ourselves and restore state
                        await self.handshake()

                    except Exception as e:
                        debug_println(f"[BG] Reconnect failed: {e}")
                        self.client         = None
                        self.characteristic = None
                        await asyncio.sleep(5)
                else:
                    debug_println("[BG] No device found — will retry in 5s")
                    await asyncio.sleep(5)

            except asyncio.CancelledError:
                debug_println("[BG] Background scanner cancelled")
                return
            except Exception as e:
                debug_println(f"[BG] Scanner error: {e}")
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Handshake — Noise identity announce + ANNOUNCE packet
    # Safe to call in offline mode (self.client may be None).
    # ------------------------------------------------------------------
    async def handshake(self):
        debug_println(" Performing handshake...")

        self.app_state = load_state()
        if self.app_state.nickname:
            self.nickname = self.app_state.nickname

        if self.client and self.characteristic:
            try:
                timestamp_ms         = int(time.time() * 1000)
                public_key_bytes     = self.encryption_service.get_public_key()
                signing_public_key_bytes = (
                    self.encryption_service.get_signing_public_key_bytes()
                )

                timestamp_data = str(timestamp_ms).encode('utf-8')
                binding_data   = (
                    self.my_peer_id.encode('utf-8')
                    + public_key_bytes
                    + timestamp_data
                )
                signature = self.encryption_service.sign_data(binding_data)

                identity_payload = self.encode_noise_identity_announcement_binary(
                    self.my_peer_id,
                    public_key_bytes,
                    signing_public_key_bytes,
                    self.nickname,
                    timestamp_ms,
                    signature,
                )

                # Current BitChat has NO separate identity announce; a peer's
                # identity (noise key + signing key) travels in the signed
                # ANNOUNCE (0x01) TLV sent below. The obsolete
                # NOISE_IDENTITY_ANNOUNCE (0x13) is ignored by current builds,
                # so we no longer emit it.
                _ = identity_payload  # (computed above; intentionally unused)

            except Exception as e:
                debug_println(f" Failed to send identity announcement: {e}")
                import traceback
                debug_println(f" Traceback: {traceback.format_exc()}")
                handshake_message = self.encryption_service.initiate_handshake(
                    self.my_peer_id
                )
                handshake_packet = create_bitchat_packet(
                    self.my_peer_id,
                    MessageType.KEY_EXCHANGE,
                    handshake_message,
                )
                await self.send_packet(handshake_packet)

            await asyncio.sleep(0.5)

            announce_packet = create_signed_bitchat_packet(
                self.my_peer_id,
                MessageType.ANNOUNCE,
                self._build_announce_payload(),
                self.encryption_service.sign_data,
                ttl=7,
            )
            await self.send_packet(announce_packet)

            debug_println(" Handshake sent. You can now chat.")
        else:
            debug_println(" No connection yet. Skipping handshake.")
            print("\033[90m» Running in offline mode. Waiting for peers...\033[0m")

        if self.app_state.nickname:
            print(f"\033[90m» Using saved nickname: {self.nickname}\033[0m")
        print("\033[90m» Type /status to see connection info\033[0m")

        self._restore_state_from_app_state()

    # ------------------------------------------------------------------
    # State restore — called after handshake() and on reconnect
    # ------------------------------------------------------------------
    def _restore_state_from_app_state(self):
        """
        Restore in-memory state from app_state.
        Called from handshake() and background_scanner() reconnect path
        so state survives disconnect/reconnect cycles.
        """
        self.blocked_peers                = self.app_state.blocked_peers
        self.channel_creators             = self.app_state.channel_creators
        self.password_protected_channels  = self.app_state.password_protected_channels
        self.channel_key_commitments      = self.app_state.channel_key_commitments

        # Restore channel keys from saved passwords
        if self.app_state.identity_key:
            for channel, enc_pw in self.app_state.encrypted_channel_passwords.items():
                try:
                    password = decrypt_password(enc_pw, self.app_state.identity_key)
                    key      = EncryptionService.derive_channel_key(password, channel)
                    self.channel_keys[channel] = key
                    debug_println(
                        f"[CHANNEL] Restored key for password-protected channel: "
                        f"{channel}"
                    )
                except Exception as e:
                    debug_println(
                        f"[CHANNEL] Failed to restore key for {channel}: {e}"
                    )

    # ------------------------------------------------------------------
    # send_packet
    # ------------------------------------------------------------------
    async def send_packet(self, packet: bytes):
        try:
            _t = MessageType(packet[1]).name if len(packet) > 1 else "?"
        except Exception:
            _t = f"0x{packet[1]:02x}" if len(packet) > 1 else "?"
        try:
            print(f"[TX] {_t} len={len(packet)}", flush=True)
        except Exception:
            pass
        debug_full_println(f"[RAW SEND] {packet.hex()}")

        if not self.client or not self.characteristic:
            debug_println("[!] No connection available. Message queued.")
            return

        if not self.client.is_connected:
            debug_println("[!] Connection lost. Cannot send packet.")
            if self.client:
                self.handle_disconnect(self.client)
            return

        if should_fragment(packet):
            await self.send_packet_with_fragmentation(packet)
            return

        write_with_response = len(packet) > 512
        try:
            await asyncio.sleep(0.01)
            await self.client.write_gatt_char(
                self.characteristic,
                packet,
                response=write_with_response,
            )
        except Exception as e:
            if "not connected" in str(e).lower():
                debug_println("[!] Lost connection while sending")
                if self.client:
                    self.handle_disconnect(self.client)
                return

            if (
                "could not complete without blocking" in str(e)
                or write_with_response
            ):
                try:
                    debug_println(
                        "[!] Write blocked, retrying without response after delay"
                    )
                    await asyncio.sleep(0.1)
                    await self.client.write_gatt_char(
                        self.characteristic,
                        packet,
                        response=False,
                    )
                    debug_println("[!] Retry successful")
                except Exception as e2:
                    if "not connected" in str(e2).lower():
                        debug_println("[!] Lost connection while sending")
                        if self.client:
                            self.handle_disconnect(self.client)
                    elif "could not complete without blocking" in str(e2):
                        debug_println(
                            "[!] Write still blocked after retry, dropping packet"
                        )
                    else:
                        raise e2
            else:
                raise e

    # ------------------------------------------------------------------
    # send_packet_with_fragmentation
    # ------------------------------------------------------------------
    async def send_packet_with_fragmentation(self, packet: bytes):
        """Fragment a large packet into current-protocol FRAGMENT (0x20) packets.

        Each fragment payload is [fragmentID:8][index:2 BE][total:2 BE]
        [originalType:1][chunk]; the reassembled chunks reconstruct the ORIGINAL
        (unpadded) packet, which the receiver parses. Preserves the original
        type, recipient and ttl so DMs route to the right peer."""
        if not self.client or not self.characteristic:
            debug_println("[!] No connection available. Cannot send fragmented message.")
            return
        if not self.client.is_connected:
            debug_println("[!] Connection lost. Cannot send fragmented packet.")
            if self.client:
                self.handle_disconnect(self.client)
            return

        core      = packet[:_unpadded_len(packet)]      # strip padding first
        orig_type = core[1] if len(core) > 1 else int(MessageType.MESSAGE)
        try:
            _p        = parse_bitchat_packet(packet)
            recipient = _p.recipient_id_str
            ttl       = max(int(_p.ttl), 3)
        except Exception:
            recipient, ttl = None, 7

        fragment_size = 150                              # -> ~193B frag packet
        chunks = [core[i:i + fragment_size]
                  for i in range(0, len(core), fragment_size)]
        total = len(chunks)
        fid   = os.urandom(8)
        print(f"[TX] FRAGMENTING {len(core)}B type=0x{orig_type:02x} -> "
              f"{total} frags (id={fid.hex()})", flush=True)

        for index, chunk in enumerate(chunks):
            frag_payload = (bytes(fid)
                            + struct.pack('>H', index)
                            + struct.pack('>H', total)
                            + bytes([orig_type])
                            + bytes(chunk))
            frag_packet = create_bitchat_packet_with_recipient(
                self.my_peer_id, recipient, MessageType.FRAGMENT,
                frag_payload, None, ttl,
            )
            try:
                await self.client.write_gatt_char(
                    self.characteristic, frag_packet, response=False,
                )
                if index < total - 1:
                    await asyncio.sleep(0.02)
            except Exception as e:
                if "not connected" in str(e).lower():
                    debug_println(f"[FRAG] connection lost on fragment {index + 1}")
                    if self.client:
                        self.handle_disconnect(self.client)
                    return
                else:
                    debug_println(f"[FRAG] fragment {index + 1} send error: {e}")

    # ------------------------------------------------------------------
    # Notification handler
    # ------------------------------------------------------------------
    async def notification_handler(
        self,
        sender: BleakGATTCharacteristic,
        data:   bytes,
    ):
        try:
            hex_string = ' '.join(f'{b:02X}' for b in data)
            debug_full_println(f"[RAW RECV] Received {len(data)} bytes")
            debug_full_println(f"[RAW RECV] {hex_string}")
        except BlockingIOError:
            pass

        try:
            packet = parse_bitchat_packet(data)

            if packet.sender_id_str == self.my_peer_id:
                return

            await self.handle_packet(packet, data)

        except Exception as e:
            try:
                print(f"[RX-ERR] failed to parse inbound packet: {e}", flush=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Packet dispatcher
    # ------------------------------------------------------------------
    async def handle_packet(self, packet: BitchatPacket, raw_data: bytes):
        try:
            if True:   # log ALL inbound while debugging registration
                _hx = ""
                if packet.msg_type in (MessageType.VERSION_HELLO,
                                       MessageType.VERSION_ACK,
                                       MessageType.NOISE_HANDSHAKE_INIT,
                                       MessageType.NOISE_HANDSHAKE_RESP):
                    _hx = f" hex={packet.payload.hex()[:200]}"
                _for_sage = (" <<< FOR-SAGE"
                             if packet.recipient_id_str == self.my_peer_id else "")
                print(f"[RX] {packet.msg_type.name} from={packet.sender_id_str} "
                      f"to={packet.recipient_id_str or '-'} ttl={packet.ttl} "
                      f"plen={len(packet.payload)}{_for_sage}{_hx}", flush=True)
        except Exception:
            pass
        if packet.msg_type == MessageType.ANNOUNCE:
            await self.handle_announce(packet)
        elif packet.msg_type == MessageType.MESSAGE:
            await self.handle_message(packet, raw_data)
        elif packet.msg_type == MessageType.FRAGMENT:
            await self.handle_fragment(packet, raw_data)
        elif packet.msg_type == MessageType.NOISE_HANDSHAKE:
            await self.handle_noise_handshake(packet)
        elif packet.msg_type == MessageType.NOISE_ENCRYPTED:
            await self.handle_noise_encrypted(packet, raw_data)
        elif packet.msg_type == MessageType.LEAVE:
            await self.handle_leave(packet)
        elif packet.msg_type == MessageType.DELIVERY_ACK:
            await self.handle_delivery_ack(packet, raw_data)
        # REQUEST_SYNC (0x21), FILE_TRANSFER (0x22) and other types are ignored.

    # ------------------------------------------------------------------
    # handle_announce
    # ------------------------------------------------------------------
    async def handle_announce(self, packet: BitchatPacket):
        _ann          = parse_announce(packet.payload)
        peer_nickname = _ann["nickname"]
        is_new_peer   = packet.sender_id_str not in self.peers

        if packet.sender_id_str not in self.peers:
            self.peers[packet.sender_id_str] = Peer()

        self.peers[packet.sender_id_str].nickname = peer_nickname
        if _ann["fingerprint"]:
            self.peers[packet.sender_id_str].fingerprint = _ann["fingerprint"]
            for _dup in [pid for pid, p in list(self.peers.items())
                         if pid != packet.sender_id_str
                         and getattr(p, "fingerprint", None) == _ann["fingerprint"]]:
                self.peers.pop(_dup, None)

        if is_new_peer:
            print(
                f"\r\033[K\033[33m{peer_nickname} connected\033[0m\n> ",
                end='', flush=True,
            )
            debug_println(
                f"[<-- RECV] Announce: Peer {packet.sender_id_str} "
                f"is now known as '{peer_nickname}'"
            )

            if False:  # PURE RESPONDER: never send our own INIT (it disrupts the
                       # phone's handshake). The phone initiates; we answer.
                debug_println(
                    f"[CRYPTO] Initiating Noise handshake with new peer "
                    f"{packet.sender_id_str}"
                )
                try:
                    handshake_message = self.encryption_service.initiate_handshake(
                        packet.sender_id_str
                    )
                    handshake_packet = create_bitchat_packet_with_recipient(
                        self.my_peer_id,
                        packet.sender_id_str,
                        MessageType.NOISE_HANDSHAKE_INIT,
                        handshake_message,
                        None,
                    )
                    # Set TTL byte (index 11 in the wire layout)
                    handshake_data     = bytearray(handshake_packet)
                    handshake_data[2] = 3
                    await self.send_packet(bytes(handshake_data))
                    debug_println(
                        f"[NOISE] Sent handshake init to {packet.sender_id_str}"
                    )
                except Exception as e:
                    debug_println(f"[CRYPTO] Failed to initiate handshake: {e}")
                    key_exchange_payload = (
                        self.encryption_service.get_combined_public_key_data()
                    )
                    key_exchange_packet = create_bitchat_packet(
                        self.my_peer_id,
                        MessageType.KEY_EXCHANGE,
                        key_exchange_payload,
                    )
                    await self.send_packet(key_exchange_packet)
            else:
                debug_println(
                    f"[CRYPTO] Sending targeted identity announce to "
                    f"{packet.sender_id_str}"
                )
                try:
                    timestamp_ms             = int(time.time() * 1000)
                    public_key_bytes         = self.encryption_service.get_public_key()
                    signing_public_key_bytes = (
                        self.encryption_service.get_signing_public_key_bytes()
                    )

                    timestamp_data = str(timestamp_ms).encode('utf-8')
                    binding_data   = (
                        self.my_peer_id.encode('utf-8')
                        + public_key_bytes
                        + timestamp_data
                    )
                    signature = self.encryption_service.sign_data(binding_data)

                    identity_payload = self.encode_noise_identity_announcement_binary(
                        self.my_peer_id,
                        public_key_bytes,
                        signing_public_key_bytes,
                        self.nickname,
                        timestamp_ms,
                        signature,
                    )

                    identity_packet = create_bitchat_packet_with_recipient(
                        self.my_peer_id,
                        packet.sender_id_str,
                        MessageType.NOISE_IDENTITY_ANNOUNCE,
                        identity_payload,
                        signature,
                    )
                    await self.send_packet(identity_packet)
                except Exception as e:
                    debug_println(
                        f"[CRYPTO] Failed to send targeted identity announce: {e}"
                    )

    # ------------------------------------------------------------------
    # handle_message
    # ------------------------------------------------------------------
    async def handle_message(self, packet: BitchatPacket, raw_data: bytes):
        fingerprint = self.encryption_service.get_peer_fingerprint(
            packet.sender_id_str
        )
        if fingerprint and fingerprint in self.blocked_peers:
            debug_println(
                f"[BLOCKED] Ignoring message from blocked peer: "
                f"{packet.sender_id_str}"
            )
            return

        is_broadcast = (
            packet.recipient_id == BROADCAST_RECIPIENT
            if packet.recipient_id else True
        )
        is_for_us = is_broadcast or (
            packet.recipient_id_str == self.my_peer_id
        )

        if not is_for_us:
            if packet.ttl > 1:
                await asyncio.sleep(random.uniform(0.01, 0.05))
                relay_data      = bytearray(raw_data)
                relay_data[2] = packet.ttl - 1   # TTL is at byte 2
                await self.send_packet(bytes(relay_data))
            return

        is_private_message = not is_broadcast and is_for_us
        decrypted_payload  = None

        if is_private_message:
            try:
                decrypted_payload = self.encryption_service.decrypt_from_peer(
                    packet.sender_id_str, packet.payload
                )
                debug_println("[PRIVATE] Successfully decrypted private message!")
            except NoiseError:
                debug_println("[PRIVATE] Failed to decrypt private message")
                return

        try:
            if is_private_message and decrypted_payload:
                unpadded = unpad_message(decrypted_payload)
                message  = parse_bitchat_message_payload(unpadded)
            else:
                # Current BitChat public messages carry the RAW UTF-8 content as
                # payload (sender = packet senderID, ts = header) rather than the
                # legacy BitchatMessage struct. Try the struct first (older
                # peers), then fall back to raw content.
                _pl = bytes(packet.payload)
                message = None
                try:
                    _m = parse_bitchat_message_payload(_pl)
                    if _m and _m.content:
                        message = _m
                except Exception:
                    message = None
                if message is None:
                    _content = _pl.decode("utf-8", errors="ignore").rstrip("\x00").strip()
                    _mid = hashlib.sha256(bytes(packet.sender_id) + _pl).hexdigest()[:16]
                    message = BitchatMessage(
                        id=_mid, content=_content, channel=None,
                        is_encrypted=False, encrypted_content=None,
                    )

            if message.id not in self.processed_messages:
                self.bloom.add(message.id)
                self.processed_messages.add(message.id)

                await self.display_message(message, packet, is_private_message)

                if should_send_ack(
                    is_private_message,
                    message.channel,
                    None,
                    self.nickname,
                    len(self.peers),
                ):
                    await self.send_delivery_ack(
                        message.id, packet.sender_id_str, is_private_message
                    )

                if packet.ttl > 1:
                    await asyncio.sleep(random.uniform(0.01, 0.05))
                    relay_data      = bytearray(raw_data)
                    relay_data[2] = packet.ttl - 1
                    await self.send_packet(bytes(relay_data))
            else:
                debug_println(
                    f"[DUPLICATE] Ignoring duplicate message: {message.id}"
                )

        except Exception as e:
            debug_full_println(f"[ERROR] Failed to parse message: {e}")

    # ------------------------------------------------------------------
    # display_message
    # ------------------------------------------------------------------
    async def display_message(
        self,
        message:    BitchatMessage,
        packet:     BitchatPacket,
        is_private: bool,
    ):
        sender_nick = (
            self.peers.get(packet.sender_id_str, Peer()).nickname
            or packet.sender_id_str
        )

        if message.channel:
            self.discovered_channels.add(message.channel)
            if message.is_encrypted:
                self.password_protected_channels.add(message.channel)

        display_content = message.content

        if message.is_encrypted and message.channel:
            if message.channel in self.channel_keys:
                try:
                    creator_fp      = self.channel_creators.get(
                        message.channel, ''
                    )
                    display_content = (
                        self.encryption_service.decrypt_from_channel(
                            message.encrypted_content,
                            message.channel,
                            self.channel_keys[message.channel],
                            creator_fp,
                        )
                    )
                except Exception:
                    display_content = "[Encrypted message - decryption failed]"
            else:
                display_content = (
                    "[Encrypted message - join channel with password]"
                )

        if is_private and display_content.startswith(COVER_TRAFFIC_PREFIX):
            debug_println(
                f"[COVER] Discarding dummy message from {sender_nick}"
            )
            return

        if is_private:
            self.chat_context.last_private_sender = (
                packet.sender_id_str, sender_nick
            )
            self.chat_context.add_dm(sender_nick, packet.sender_id_str)

        timestamp = datetime.now()
        display   = format_message_display(
            timestamp,
            sender_nick,
            display_content,
            is_private,
            bool(message.channel),
            message.channel,
            self.nickname if is_private else None,
            self.nickname,
        )

        print(f"\r\033[K{display}")

        if is_private and not isinstance(
            self.chat_context.current_mode, PrivateDM
        ):
            print("\033[90m» /reply to respond\033[0m")

        print("> ", end='', flush=True)

    # ------------------------------------------------------------------
    # handle_fragment
    # ------------------------------------------------------------------
    async def handle_fragment(self, packet: BitchatPacket, raw_data: bytes):
        # Current BitChat fragment payload:
        #   [fragmentID:8][index:2 BE][total:2 BE][originalType:1][data...]
        # The reassembled bytes are the ORIGINAL PACKET'S PAYLOAD; we rebuild a
        # logical packet of `originalType` (sender/recipient inherited from the
        # fragment) and dispatch it.
        if len(packet.payload) >= 13:
            fragment_id   = bytes(packet.payload[0:8])
            index         = struct.unpack('>H', packet.payload[8:10])[0]
            total         = struct.unpack('>H', packet.payload[10:12])[0]
            original_type = packet.payload[12]
            fragment_data = bytes(packet.payload[13:])

            result = self.fragment_collector.add_fragment(
                fragment_id,
                index,
                total,
                original_type,
                fragment_data,
                packet.sender_id_str,
            )

            if result:
                complete_data = result[0]
                data_bytes = (complete_data if isinstance(complete_data, bytes)
                              else bytes(complete_data))
                try:
                    _otn = MessageType(original_type).name
                except ValueError:
                    _otn = f"0x{original_type:02x}"
                print(f"[FRAG] reassembled origType={_otn} len={len(data_bytes)} "
                      f"hex={data_bytes[:48].hex()}", flush=True)
                # Reassembled bytes are either a full re-encoded packet or the
                # original packet's PAYLOAD (of original_type). Try a full-packet
                # decode first (it handles its own decompression); otherwise
                # dispatch a synthetic packet carrying the payload.
                dispatched = False
                try:
                    fp = parse_bitchat_packet(data_bytes)
                    if fp is not None and int(fp.msg_type) == original_type:
                        await self.handle_packet(fp, data_bytes)
                        dispatched = True
                except Exception:
                    dispatched = False
                if not dispatched:
                    try:
                        _mt = MessageType(original_type)
                    except ValueError:
                        _mt = MessageType.MESSAGE
                    reassembled = BitchatPacket(
                        msg_type         = _mt,
                        sender_id        = packet.sender_id,
                        sender_id_str    = packet.sender_id_str,
                        recipient_id     = packet.recipient_id,
                        recipient_id_str = packet.recipient_id_str,
                        payload          = data_bytes,
                        ttl              = packet.ttl,
                    )
                    try:
                        await self.handle_packet(reassembled, data_bytes)
                    except Exception as e:
                        debug_full_println(f"[FRAG] dispatch failed: {e}")

        if packet.ttl > 1:
            await asyncio.sleep(random.uniform(0.01, 0.05))
            relay_data      = bytearray(raw_data)
            relay_data[2] = packet.ttl - 1
            await self.send_packet(bytes(relay_data))

    # ------------------------------------------------------------------
    # handle_noise_handshake  (current protocol: a single message type 0x10
    # carries every Noise XX stage; the encryption service tracks which stage
    # we are in, so one handler serves both initiator and responder roles.)
    # ------------------------------------------------------------------
    async def handle_noise_handshake(self, packet: BitchatPacket):
        if (packet.recipient_id_str
                and packet.recipient_id_str != self.my_peer_id):
            # v2.12.5: never ignore silently -- a recipient mismatch here is
            # exactly how the NUL-trim bug hid for months.
            try:
                if packet.recipient_id != BROADCAST_RECIPIENT:
                    print(f"[NOISE] handshake addressed to "
                          f"{packet.recipient_id_str}, I am {self.my_peer_id}"
                          f" -- ignoring (peer has a stale ID for us?)",
                          flush=True)
            except Exception:
                pass
            return
        try:
            payload_bytes = (bytes(packet.payload)
                             if isinstance(packet.payload, bytearray)
                             else packet.payload)
            response = self.encryption_service.process_handshake_message(
                packet.sender_id_str, payload_bytes
            )
            if response:
                resp = create_bitchat_packet_with_recipient(
                    self.my_peer_id, packet.sender_id_str,
                    MessageType.NOISE_HANDSHAKE, response, None,
                )
                resp = bytearray(resp)
                resp[2] = 3   # TTL
                await self.send_packet(bytes(resp))
                print(f"[NOISE] handshake step -> {packet.sender_id_str} "
                      f"({len(response)}B)", flush=True)
            if self.encryption_service.is_session_established(
                    packet.sender_id_str):
                self.handshake_attempt_times.pop(packet.sender_id_str, None)
                nick = (self.peers.get(packet.sender_id_str, Peer()).nickname
                        or packet.sender_id_str)
                print(f"\r\033[K\033[92m\u2713 Secure session established with "
                      f"{nick}\033[0m\n> ", end='', flush=True)
                await asyncio.sleep(0.1)
                await self.send_pending_private_messages(packet.sender_id_str)
        except Exception as e:
            print(f"[NOISE] handshake from {packet.sender_id_str} FAILED: {e}",
                  flush=True)
            self.encryption_service.clear_handshake_state(packet.sender_id_str)

    # ------------------------------------------------------------------
    # handle_key_exchange
    # ------------------------------------------------------------------
    async def handle_key_exchange(self, packet: BitchatPacket):
        try:
            payload_bytes = (
                bytes(packet.payload)
                if isinstance(packet.payload, bytearray)
                else packet.payload
            )
            response = self.encryption_service.process_handshake_message(
                packet.sender_id_str, payload_bytes
            )
            if response:
                response_packet = create_bitchat_packet(
                    self.my_peer_id, MessageType.KEY_EXCHANGE, response
                )
                await self.send_packet(response_packet)

            if self.encryption_service.is_session_established(
                packet.sender_id_str
            ):
                debug_println(
                    f"[CRYPTO] Handshake completed with {packet.sender_id_str}"
                )

        except Exception as e:
            debug_println(
                f"[CRYPTO] Handshake failed with {packet.sender_id_str}: {e}"
            )

    # ------------------------------------------------------------------
    # handle_noise_handshake_init
    # ------------------------------------------------------------------
    async def handle_noise_handshake_init(self, packet: BitchatPacket):
        debug_println(
            f"[NOISE] Received handshake init from {packet.sender_id_str}"
        )

        if (
            packet.recipient_id_str
            and packet.recipient_id_str != self.my_peer_id
        ):
            try:
                if packet.recipient_id != BROADCAST_RECIPIENT:
                    print(f"[NOISE] handshake INIT addressed to "
                          f"{packet.recipient_id_str}, I am {self.my_peer_id}"
                          f" -- ignoring", flush=True)
            except Exception:
                pass
            return

        # Handshake collision: we initiated AND the phone also sent an INIT.
        # The phone stays a committed initiator (it does NOT reset to responder
        # or answer our INIT), so WE defer: drop our initiator state and answer
        # as responder. With the prologue + transport fixes our RESP is now valid,
        # so the phone accepts it and sends msg3 -> the session completes.
        # (A RESPONDER state here means this INIT is really msg3 -> fall through.)
        _hs = self.encryption_service.handshake_states.get(packet.sender_id_str)
        if _hs is not None and getattr(_hs, "role", None) == "initiator":
            print(f"[NOISE] collision with {packet.sender_id_str} -> "
                  f"deferring, becoming responder", flush=True)
            self.encryption_service.clear_handshake_state(packet.sender_id_str)

        try:
            payload_bytes = (
                bytes(packet.payload)
                if isinstance(packet.payload, bytearray)
                else packet.payload
            )
            response = self.encryption_service.process_handshake_message(
                packet.sender_id_str, payload_bytes
            )

            if response:
                response_packet = create_bitchat_packet_with_recipient(
                    self.my_peer_id,
                    packet.sender_id_str,
                    MessageType.NOISE_HANDSHAKE_RESP,
                    response,
                    None,
                )
                resp_data      = bytearray(response_packet)
                resp_data[2]  = 3   # TTL
                await self.send_packet(bytes(resp_data))
                debug_println(
                    f"[NOISE] Sent handshake response to "
                    f"{packet.sender_id_str}, payload size: {len(response)}"
                )

            if self.encryption_service.is_session_established(
                packet.sender_id_str
            ):
                debug_println(
                    f"[NOISE] Handshake completed with {packet.sender_id_str}"
                )
                self.handshake_attempt_times.pop(packet.sender_id_str, None)
                peer_nickname = (
                    self.peers.get(packet.sender_id_str, Peer()).nickname
                    or packet.sender_id_str
                )
                print(
                    f"\r\033[K\033[92m✓ Secure session established with "
                    f"{peer_nickname}\033[0m"
                )
                print("> ", end='', flush=True)
                await asyncio.sleep(0.1)
                await self.send_pending_private_messages(packet.sender_id_str)

        except Exception as e:
            try:
                print(f"[NOISE] handshake INIT from {packet.sender_id_str} "
                      f"FAILED: {e}", flush=True)
            except Exception:
                pass
            self.encryption_service.clear_handshake_state(packet.sender_id_str)

    # ------------------------------------------------------------------
    # handle_noise_handshake_resp
    # ------------------------------------------------------------------
    async def handle_noise_handshake_resp(self, packet: BitchatPacket):
        debug_println(
            f"[NOISE] Received handshake response from {packet.sender_id_str}"
        )

        if (
            packet.recipient_id_str
            and packet.recipient_id_str != self.my_peer_id
        ):
            debug_println("[NOISE] Handshake response not for us, ignoring")
            return

        try:
            payload_bytes = (
                bytes(packet.payload)
                if isinstance(packet.payload, bytearray)
                else packet.payload
            )
            response = self.encryption_service.process_handshake_message(
                packet.sender_id_str, payload_bytes
            )

            if response:
                final_packet = create_bitchat_packet_with_recipient(
                    self.my_peer_id,
                    packet.sender_id_str,
                    MessageType.NOISE_HANDSHAKE_INIT,
                    response,
                    None,
                )
                final_data      = bytearray(final_packet)
                final_data[2]  = 3   # TTL
                await self.send_packet(bytes(final_data))
                debug_println(
                    f"[NOISE] Sent final handshake message to "
                    f"{packet.sender_id_str}"
                )

            if self.encryption_service.is_session_established(
                packet.sender_id_str
            ):
                debug_println(
                    f"[NOISE] Handshake completed with {packet.sender_id_str}"
                )
                self.handshake_attempt_times.pop(packet.sender_id_str, None)
                peer_nickname = (
                    self.peers.get(packet.sender_id_str, Peer()).nickname
                    or packet.sender_id_str
                )
                print(
                    f"\r\033[K\033[92m✓ Secure session established with "
                    f"{peer_nickname}\033[0m"
                )
                print("> ", end='', flush=True)
                await asyncio.sleep(0.1)
                await self.send_pending_private_messages(packet.sender_id_str)

        except Exception as e:
            try:
                print(f"[NOISE] handshake RESP from {packet.sender_id_str} "
                      f"FAILED: {e}", flush=True)
            except Exception:
                pass
            self.encryption_service.clear_handshake_state(packet.sender_id_str)

    # ------------------------------------------------------------------
    # handle_noise_encrypted
    # ------------------------------------------------------------------
    async def handle_noise_encrypted(
        self, packet: BitchatPacket, raw_data: bytes
    ):
        debug_println(
            f"[NOISE] Received encrypted message from {packet.sender_id_str}"
        )

        fingerprint = self.encryption_service.get_peer_fingerprint(
            packet.sender_id_str
        )
        if fingerprint and fingerprint in self.blocked_peers:
            debug_println(
                f"[BLOCKED] Ignoring encrypted message from blocked peer: "
                f"{packet.sender_id_str}"
            )
            return

        try:
            payload_bytes = (
                bytes(packet.payload)
                if isinstance(packet.payload, bytearray)
                else packet.payload
            )
            decrypted_payload = self.encryption_service.decrypt_from_peer(
                packet.sender_id_str, payload_bytes
            )
            debug_println(
                f"[NOISE] Successfully decrypted {len(decrypted_payload)} "
                f"bytes from {packet.sender_id_str}"
            )

            try:
                # Current BitChat noiseEncrypted plaintext framing:
                #   [NoisePayloadType:1][payload]
                #     0x01 privateMessage -> PrivateMessagePacket TLV
                #                            (0x00 messageID, 0x01 content)
                #     0x02 readReceipt / 0x03 delivered -> UTF-8 message id
                dp    = bytes(decrypted_payload)
                ptype = dp[0] if dp else 0
                body  = dp[1:]
                if ptype == 0x01:
                    mid, content = None, None
                    i = 0
                    while i + 2 <= len(body):
                        t  = body[i]
                        ln = body[i + 1]
                        if i + 2 + ln > len(body):
                            break
                        val = body[i + 2:i + 2 + ln]
                        if t == 0x00:
                            mid = val.decode('utf-8', errors='ignore')
                        elif t == 0x01:
                            content = val.decode('utf-8', errors='ignore')
                        i += 2 + ln
                    if content is not None:
                        _mid = mid or hashlib.sha256(dp).hexdigest()[:16]
                        if _mid not in self.processed_messages:
                            self.bloom.add(_mid)
                            self.processed_messages.add(_mid)
                            # BitChat sends internal control DMs as plain text
                            # ([FAVORITED]/[UNFAVORITED] favorite sync, receipts).
                            # Ack them but keep them out of the human feed.
                            _c = content.lstrip()
                            _is_control = (
                                _c.startswith("[FAVORITED]")
                                or _c.startswith("[UNFAVORITED]")
                                or _c.startswith("[DELIVERED]")
                                or _c.startswith("[READ]")
                                or _c.startswith("[NOISE")
                            )
                            if not _is_control:
                                print(f"[DM] {packet.sender_id_str}: {content}",
                                      flush=True)
                                message = BitchatMessage(
                                    id=_mid, content=content, channel=None,
                                    is_encrypted=False, encrypted_content=None,
                                )
                                await self.display_message(message, packet, True)
                            await self.send_delivery_ack(
                                _mid, packet.sender_id_str, True
                            )
                    else:
                        debug_println("[NOISE] privateMessage TLV had no content")
                elif ptype in (0x02, 0x03):
                    debug_println(
                        f"[NOISE] receipt type=0x{ptype:02x} for "
                        f"{body.decode('utf-8', errors='ignore')}"
                    )
                else:
                    debug_println(
                        f"[NOISE] unknown NoisePayloadType 0x{ptype:02x}"
                    )
            except Exception as e:
                debug_println(
                    f"[NOISE] Error parsing decrypted payload: {e}"
                )

        except Exception as e:
            debug_println(
                f"[NOISE] Failed to decrypt message from "
                f"{packet.sender_id_str}: {e}"
            )
            if not self.encryption_service.is_session_established(
                packet.sender_id_str
            ):
                debug_println(
                    f"[NOISE] No session established with "
                    f"{packet.sender_id_str}"
                )
            else:
                debug_println(
                    "[NOISE] Session exists but decryption failed — "
                    "possible key sync issue"
                )
                if "InvalidTag" in str(e):
                    debug_println(
                        "[NOISE] InvalidTag suggests nonce desync — "
                        "likely iOS acknowledgment"
                    )

    # ------------------------------------------------------------------
    # handle_leave
    # ------------------------------------------------------------------
    async def handle_leave(self, packet: BitchatPacket):
        payload_str = packet.payload.decode('utf-8', errors='ignore').strip()

        if payload_str.startswith('#'):
            channel     = payload_str
            sender_nick = (
                self.peers.get(packet.sender_id_str, Peer()).nickname
                or packet.sender_id_str
            )

            if (
                isinstance(self.chat_context.current_mode, Channel)
                and self.chat_context.current_mode.name == channel
            ):
                print(
                    f"\r\033[K\033[90m« {sender_nick} left "
                    f"{channel}\033[0m\n> ",
                    end='', flush=True,
                )

            debug_println(
                f"[<-- RECV] {sender_nick} left channel {channel}"
            )
        else:
            disconnected_peer = self.peers.pop(packet.sender_id_str, None)
            if disconnected_peer and disconnected_peer.nickname:
                print(
                    f"\r\033[K\033[33m{disconnected_peer.nickname} "
                    f"disconnected\033[0m\n> ",
                    end='', flush=True,
                )

                if disconnected_peer.nickname in self.chat_context.active_dms:
                    del self.chat_context.active_dms[
                        disconnected_peer.nickname
                    ]

                if packet.sender_id_str in self.pending_private_messages:
                    del self.pending_private_messages[packet.sender_id_str]

                self.encryption_service.remove_session(packet.sender_id_str)
                debug_println(
                    f"[NOISE] Cleared session for disconnected peer "
                    f"{packet.sender_id_str}"
                )

                if (
                    isinstance(self.chat_context.current_mode, PrivateDM)
                    and self.chat_context.current_mode.peer_id
                    == packet.sender_id_str
                ):
                    self.chat_context.switch_to_public()
                    print(
                        "\033[90m» Switched to public chat "
                        "(peer disconnected)\033[0m\n> ",
                        end='', flush=True,
                    )

            debug_println(
                f"[<-- RECV] Peer {packet.sender_id_str} "
                f"({payload_str}) has left"
            )

            if len(self.peers) == 0:
                print(
                    "\033[90m» You're now the only one in the "
                    "network.\033[0m\n> ",
                    end='', flush=True,
                )

    # ------------------------------------------------------------------
    # handle_channel_announce
    # ------------------------------------------------------------------
    async def handle_channel_announce(self, packet: BitchatPacket):
        payload_str = packet.payload.decode('utf-8', errors='ignore')
        parts       = payload_str.split('|')

        if len(parts) >= 3:
            channel      = parts[0]
            is_protected = parts[1] == '1'
            creator_id   = parts[2]
            key_commitment = parts[3] if len(parts) > 3 else ""

            debug_println(
                f"[<-- RECV] Channel announce: {channel} "
                f"(protected: {is_protected}, owner: {creator_id})"
            )

            if creator_id:
                self.channel_creators[channel] = creator_id

            if is_protected:
                self.password_protected_channels.add(channel)
                if key_commitment:
                    self.channel_key_commitments[channel] = key_commitment
            else:
                self.password_protected_channels.discard(channel)
                self.channel_keys.pop(channel, None)
                self.channel_key_commitments.pop(channel, None)

            self.chat_context.add_channel(channel)
            await self.save_app_state()

    # ------------------------------------------------------------------
    # handle_delivery_ack
    # ------------------------------------------------------------------
    async def handle_delivery_ack(
        self, packet: BitchatPacket, raw_data: bytes
    ):
        is_for_us = (
            packet.recipient_id_str == self.my_peer_id
            if packet.recipient_id_str
            else False
        )

        if is_for_us:
            ack_payload = packet.payload
            if (
                packet.ttl == 3
                and self.encryption_service.is_session_established(
                    packet.sender_id_str
                )
            ):
                try:
                    ack_payload = self.encryption_service.decrypt_from_peer(
                        packet.sender_id_str, packet.payload
                    )
                except Exception:
                    pass

            try:
                ack_data = json.loads(ack_payload)
                ack = DeliveryAck(
                    original_message_id = ack_data['originalMessageID'],
                    ack_id              = ack_data['ackID'],
                    recipient_id        = ack_data['recipientID'],
                    recipient_nickname  = ack_data['recipientNickname'],
                    timestamp           = ack_data['timestamp'],
                    hop_count           = ack_data['hopCount'],
                )

                if self.delivery_tracker.mark_delivered(
                    ack.original_message_id
                ):
                    print(
                        f"\r\033[K\033[90m✓ Delivered to "
                        f"{ack.recipient_nickname}\033[0m\n> ",
                        end='', flush=True,
                    )

            except Exception as e:
                debug_println(f"[ACK] Failed to parse delivery ACK: {e}")

        elif packet.ttl > 1:
            relay_data      = bytearray(raw_data)
            relay_data[2] = packet.ttl - 1
            await self.send_packet(bytes(relay_data))

    # ------------------------------------------------------------------
    # handle_noise_identity_announce
    # ------------------------------------------------------------------
    async def handle_noise_identity_announce(self, packet: BitchatPacket):
        try:
            sender_id = packet.sender_id_str
            debug_println(
                f"[NOISE] Received identity announcement from {sender_id}"
            )

            if sender_id == self.my_peer_id:
                return

            announcement = None

            try:
                announcement = self.parse_noise_identity_announcement_binary(
                    packet.payload
                )
            except Exception as be:
                debug_println(f"[NOISE] Binary decode failed: {be}")
                try:
                    announcement_data = json.loads(
                        packet.payload.decode('utf-8')
                    )
                    announcement = {
                        'peerID':          announcement_data.get(
                            'peerID', sender_id
                        ),
                        'nickname':        announcement_data.get(
                            'nickname', 'Unknown'
                        ),
                        'publicKey':       announcement_data.get(
                            'publicKey', ''
                        ),
                        'signingPublicKey': announcement_data.get(
                            'signingPublicKey', ''
                        ),
                        'timestamp':       announcement_data.get(
                            'timestamp', 0
                        ),
                        'signature':       announcement_data.get(
                            'signature', ''
                        ),
                    }
                except Exception as je:
                    debug_println(
                        f"[NOISE] JSON decode also failed: {je}"
                    )
                    debug_println(
                        f"[NOISE] Raw payload (first 32 bytes): "
                        f"{packet.payload[:32].hex()}"
                    )
                    return

            if not announcement:
                debug_println(
                    f"[NOISE] Failed to decode identity announcement "
                    f"from {sender_id}"
                )
                return

            peer_id  = announcement['peerID']
            nickname = clean_nickname(announcement.get('nickname') or '')

            # Stable identity = SHA-256(Noise static public key).
            fingerprint = None
            try:
                _pk = announcement.get('publicKey') or ''
                if _pk:
                    fingerprint = hashlib.sha256(bytes.fromhex(_pk)).hexdigest()
            except Exception:
                fingerprint = None

            # Peer rotated its ephemeral ID? Migrate the old entry.
            _prev = announcement.get('previousPeerID')
            if _prev and _prev != peer_id and _prev in self.peers:
                _old = self.peers.pop(_prev, None)
                if _old is not None:
                    if not nickname and _old.nickname:
                        nickname = _old.nickname
                    if fingerprint is None:
                        fingerprint = getattr(_old, 'fingerprint', None)
                debug_println(f"[NOISE] peer {_prev} rotated -> {peer_id}; merged")

            debug_println(
                f"[NOISE] Identity announcement: {peer_id} -> {nickname}"
            )

            is_new_peer = peer_id not in self.peers
            if peer_id not in self.peers:
                self.peers[peer_id] = Peer()
            self.peers[peer_id].nickname = nickname
            if fingerprint:
                self.peers[peer_id].fingerprint = fingerprint
                # Collapse any stale entries that share this fingerprint.
                for _dup in [pid for pid, p in list(self.peers.items())
                             if pid != peer_id
                             and getattr(p, 'fingerprint', None) == fingerprint]:
                    self.peers.pop(_dup, None)

            if is_new_peer:
                print(
                    f"\r\033[K\033[33m{nickname} connected\033[0m\n> ",
                    end='', flush=True,
                )
                debug_println(
                    f"[<-- RECV] Announce: Peer {peer_id} is now "
                    f"known as '{nickname}'"
                )

            if False:  # PURE RESPONDER: never send our own INIT (it disrupts the
                       # phone's handshake). The phone initiates; we answer.
                debug_println(
                    f"[NOISE] We should initiate handshake with {peer_id}"
                )
                if not self.encryption_service.is_session_established(peer_id):
                    try:
                        handshake_message = (
                            self.encryption_service.initiate_handshake(peer_id)
                        )
                        handshake_packet = create_bitchat_packet_with_recipient(
                            self.my_peer_id,
                            peer_id,
                            MessageType.NOISE_HANDSHAKE_INIT,
                            handshake_message,
                            None,
                        )
                        hs_data      = bytearray(handshake_packet)
                        hs_data[2]  = 3   # TTL
                        await self.send_packet(bytes(hs_data))
                        debug_println(
                            f"[NOISE] Initiated handshake with {peer_id}"
                        )
                    except Exception as e:
                        debug_println(
                            f"[NOISE] Failed to initiate handshake: {e}"
                        )
            else:
                debug_println(
                    f"[NOISE] Waiting for {peer_id} to initiate handshake"
                )

        except Exception as e:
            debug_println(
                f"[NOISE] Error handling identity announcement: {e}"
            )
            import traceback
            debug_println(
                f"[NOISE] Identity announce error details: "
                f"{traceback.format_exc()}"
            )

    # ------------------------------------------------------------------
    # parse_noise_identity_announcement_binary
    # ------------------------------------------------------------------
    def parse_noise_identity_announcement_binary(
        self, data: bytes
    ) -> Optional[dict]:
        """
        Parse binary format Noise identity announcement matching iOS
        appendData format. 1-byte length prefixes throughout.
        """
        try:
            offset = 0

            debug_println(
                f"[NOISE] Parsing binary announcement, "
                f"total length: {len(data)}"
            )

            if offset >= len(data):
                debug_println("[NOISE] Error: Not enough data for flags")
                return None
            flags  = data[offset]; offset += 1
            has_previous_peer_id = (flags & 0x01) != 0

            # peerID — 8 bytes
            if offset + 8 > len(data):
                debug_println("[NOISE] Error: Not enough data for peerID")
                return None
            peer_id  = data[offset:offset + 8].hex(); offset += 8

            # publicKey — 1-byte length prefix
            if offset >= len(data):
                debug_println(
                    "[NOISE] Error: Not enough data for publicKey length"
                )
                return None
            pub_key_len = data[offset]; offset += 1
            if offset + pub_key_len > len(data):
                debug_println("[NOISE] Error: Not enough data for publicKey")
                return None
            public_key = data[offset:offset + pub_key_len]; offset += pub_key_len

            # signingPublicKey — 1-byte length prefix
            if offset >= len(data):
                debug_println(
                    "[NOISE] Error: Not enough data for signingPublicKey length"
                )
                return None
            signing_key_len = data[offset]; offset += 1
            if offset + signing_key_len > len(data):
                debug_println(
                    "[NOISE] Error: Not enough data for signingPublicKey"
                )
                return None
            signing_public_key = (
                data[offset:offset + signing_key_len]
            ); offset += signing_key_len

            # nickname — 1-byte length prefix
            if offset >= len(data):
                debug_println(
                    "[NOISE] Error: Not enough data for nickname length"
                )
                return None
            nickname_len = data[offset]; offset += 1
            nickname = ""
            if nickname_len > 0:
                if offset + nickname_len > len(data):
                    debug_println(
                        "[NOISE] Error: Not enough data for nickname"
                    )
                    return None
                nickname = data[offset:offset + nickname_len].decode('utf-8')
                offset  += nickname_len

            # timestamp — 8-byte UInt64 big-endian milliseconds
            if offset + 8 > len(data):
                debug_println(
                    "[NOISE] Error: Not enough data for timestamp"
                )
                return None
            timestamp_ms = int.from_bytes(
                data[offset:offset + 8], byteorder='big'
            ); offset += 8

            # previousPeerID — 8 bytes if flag set
            previous_peer_id = None
            if has_previous_peer_id:
                if offset + 8 > len(data):
                    debug_println(
                        "[NOISE] Error: Not enough data for previousPeerID"
                    )
                    return None
                previous_peer_id = data[offset:offset + 8].hex()
                offset += 8

            # signature — 1-byte length prefix
            if offset >= len(data):
                debug_println(
                    "[NOISE] Error: Not enough data for signature length"
                )
                return None
            signature_len = data[offset]; offset += 1
            if offset + signature_len > len(data):
                debug_println("[NOISE] Error: Not enough data for signature")
                return None
            signature = data[offset:offset + signature_len]
            offset   += signature_len

            debug_println(
                f"[NOISE] Total parsed {offset} bytes out of "
                f"{len(data)} available"
            )

            return {
                'peerID':          peer_id,
                'publicKey':       public_key.hex(),
                'signingPublicKey': signing_public_key.hex(),
                'nickname':        nickname,
                'timestamp':       timestamp_ms / 1000.0,
                'signature':       signature.hex(),
                'previousPeerID':  previous_peer_id,
                'truncated':       False,
            }

        except Exception as e:
            debug_println(
                f"[NOISE] Error parsing binary announcement: {e}"
            )
            import traceback
            debug_println(
                f"[NOISE] Binary parser error details: "
                f"{traceback.format_exc()}"
            )
            return None

    # ------------------------------------------------------------------
    # encode_noise_identity_announcement_binary
    # ------------------------------------------------------------------
    def encode_noise_identity_announcement_binary(
        self,
        peer_id:             str,
        public_key:          bytes,
        signing_public_key:  bytes,
        nickname:            str,
        timestamp_ms:        int,
        signature:           bytes,
        previous_peer_id:    Optional[str] = None,
    ) -> bytes:
        """
        Encode Noise identity announcement to binary format matching iOS
        appendData format. timestamp_ms is already in milliseconds —
        passed directly, NOT multiplied by 1000 again.
        """
        data = bytearray()

        # flags byte: bit 0 = hasPreviousPeerID
        flags = 0x01 if previous_peer_id else 0x00
        data.append(flags)

        # peerID — 8 bytes
        peer_data = bytes.fromhex(peer_id.ljust(16, '0')[:16])
        data.extend(peer_data)

        # publicKey — 1-byte length prefix
        data.append(len(public_key))
        data.extend(public_key)

        # signingPublicKey — 1-byte length prefix
        data.append(len(signing_public_key))
        data.extend(signing_public_key)

        # nickname — 1-byte length prefix
        nickname_bytes = nickname.encode('utf-8')
        data.append(len(nickname_bytes))
        data.extend(nickname_bytes)

        # timestamp — 8-byte UInt64 big-endian milliseconds
        data.extend(timestamp_ms.to_bytes(8, byteorder='big'))

        # previousPeerID — 8 bytes if present
        if previous_peer_id:
            prev_data = bytes.fromhex(
                previous_peer_id.ljust(16, '0')[:16]
            )
            data.extend(prev_data)

        # signature — 1-byte length prefix
        data.append(len(signature))
        data.extend(signature)

        return bytes(data)

    # ------------------------------------------------------------------
    # send_delivery_ack
    # ------------------------------------------------------------------
    async def send_delivery_ack(
        self,
        message_id: str,
        sender_id:  str,
        is_private: bool,
    ):
        ack_id = f"{message_id}-{self.my_peer_id}"
        if not self.delivery_tracker.should_send_ack(ack_id):
            return

        debug_println(
            f"[ACK] Sending delivery ACK for message {message_id}"
        )

        ack = DeliveryAck(
            original_message_id = message_id,
            ack_id              = str(uuid.uuid4()),
            recipient_id        = self.my_peer_id,
            recipient_nickname  = self.nickname,
            timestamp           = int(time.time() * 1000),
            hop_count           = 1,
        )

        ack_payload = json.dumps({
            'originalMessageID': ack.original_message_id,
            'ackID':             ack.ack_id,
            'recipientID':       ack.recipient_id,
            'recipientNickname': ack.recipient_nickname,
            'timestamp':         ack.timestamp,
            'hopCount':          ack.hop_count,
        }).encode()

        if is_private:
            try:
                ack_payload = self.encryption_service.encrypt(
                    ack_payload, sender_id
                )
            except Exception:
                pass

        ack_packet = create_bitchat_packet_with_recipient(
            self.my_peer_id,
            sender_id,
            MessageType.DELIVERY_ACK,
            ack_payload,
            None,
        )
        ack_data      = bytearray(ack_packet)
        ack_data[2]  = 3   # TTL
        await self.send_packet(bytes(ack_data))

    # ------------------------------------------------------------------
    # send_channel_announce
    # ------------------------------------------------------------------
    async def send_channel_announce(
        self,
        channel:        str,
        is_protected:   bool,
        key_commitment: Optional[str],
    ):
        payload = (
            f"{channel}|{'1' if is_protected else '0'}"
            f"|{self.my_peer_id}|{key_commitment or ''}"
        )
        packet = create_bitchat_packet(
            self.my_peer_id,
            MessageType.CHANNEL_ANNOUNCE,
            payload.encode(),
        )
        pkt_data      = bytearray(packet)
        pkt_data[2]  = 5   # TTL
        debug_println(f"[CHANNEL] Sending channel announce for {channel}")
        await self.send_packet(bytes(pkt_data))

    # ------------------------------------------------------------------
    # save_app_state
    # ------------------------------------------------------------------
    async def save_app_state(self):
        self.app_state.nickname                   = self.nickname
        self.app_state.blocked_peers              = self.blocked_peers
        self.app_state.channel_creators           = self.channel_creators
        self.app_state.joined_channels            = self.chat_context.active_channels
        self.app_state.password_protected_channels = (
            self.password_protected_channels
        )
        self.app_state.channel_key_commitments    = self.channel_key_commitments

        try:
            save_state(self.app_state)
        except Exception as e:
            logging.error(f"Failed to save state: {e}")

    # ------------------------------------------------------------------
    # handle_user_input
    # ------------------------------------------------------------------
    async def handle_user_input(self, line: str):
        # Quick numeric conversation switch
        if len(line) == 1 and line.isdigit():
            num = int(line)
            if self.chat_context.switch_to_number(num):
                debug_println(self.chat_context.get_status_line())
            else:
                print("» Invalid conversation number")
            return

        if line == "/help":
            print_help()
            return

        if line == "/exit" or line == "/quit":
            if self.client and self.client.is_connected:
                leave_packet = create_bitchat_packet(
                    self.my_peer_id,
                    MessageType.LEAVE,
                    self.nickname.encode(),
                )
                await self.send_packet(leave_packet)
                await asyncio.sleep(0.1)
            await self.save_app_state()
            self.running = False
            return

        if line.startswith("/name ") or line.startswith("/nick "):
            # support both /name and /nick aliases
            new_name = line.split(None, 1)[1].strip() if ' ' in line else ""
            if not new_name:
                print("\033[93m⚠ Usage: /name <new_nickname>\033[0m")
                print("\033[90mExample: /name Alice\033[0m")
            elif len(new_name) > 20:
                print("\033[93m⚠ Nickname too long (max 20 characters)\033[0m")
            elif not all(c.isalnum() or c in '-_' for c in new_name):
                print("\033[93m⚠ Nicknames can only contain letters, "
                      "numbers, hyphens and underscores.\033[0m")
            elif new_name in ("system", "all"):
                print("\033[93m⚠ That nickname is reserved.\033[0m")
            else:
                self.nickname = new_name
                announce_packet = create_bitchat_packet(
                    self.my_peer_id,
                    MessageType.ANNOUNCE,
                    self._build_announce_payload(),
                )
                await self.send_packet(announce_packet)
                print(
                    f"\033[90m» Nickname changed to: {self.nickname}\033[0m"
                )
                await self.save_app_state()
            return

        if line == "/list":
            self.chat_context.show_conversation_list()
            return

        if line == "/switch":
            print(f"\n{self.chat_context.get_conversation_list_with_numbers()}")
            switch_input = await aioconsole.ainput(
                "Enter number to switch to: "
            )
            if switch_input.strip().isdigit():
                num = int(switch_input.strip())
                if self.chat_context.switch_to_number(num):
                    debug_println(self.chat_context.get_status_line())
                else:
                    print("» Invalid selection")
            return

        if line.startswith("/j "):
            await self.handle_join_channel(line)
            return

        if line == "/public":
            self.chat_context.switch_to_public()
            debug_println(self.chat_context.get_status_line())
            return

        if line in ("/online", "/w", "/peers"):
            if not self.client or not self.client.is_connected:
                print("» You're not connected to any peers yet.")
                print("\033[90mWaiting for other BitChat devices...\033[0m")
            else:
                online_list = [
                    p.nickname for p in self.peers.values() if p.nickname
                ]
                if online_list:
                    print(
                        f"» Online users: {', '.join(sorted(online_list))}"
                    )
                else:
                    print("» No one else is online right now.")
            print("> ", end='', flush=True)
            return

        if line == "/channels":
            all_channels = (
                set(self.chat_context.active_channels)
                | set(self.channel_keys.keys())
                | self.discovered_channels
            )
            if all_channels:
                print("» Discovered channels:")
                for channel in sorted(all_channels):
                    status = ""
                    if channel in self.chat_context.active_channels:
                        status += " ✓"
                    if channel in self.password_protected_channels:
                        status += " 🔒"
                        if channel in self.channel_keys:
                            status += " 🔑"
                    print(f"  {channel}{status}")
                print(
                    "\n✓ = joined, 🔒 = password protected, 🔑 = authenticated"
                )
            else:
                print(
                    "» No channels discovered yet. "
                    "Channels appear as people use them."
                )
            print("> ", end='', flush=True)
            return

        if line == "/status":
            peer_count        = len(self.peers)
            channel_count     = len(self.chat_context.active_channels)
            dm_count          = len(self.chat_context.active_dms)
            connection_status = (
                "Connected"
                if (self.client and self.client.is_connected)
                else "Offline"
            )
            session_count      = self.encryption_service.get_session_count()
            pending_handshakes = len(self.encryption_service.handshake_states)
            pending_messages   = sum(
                len(msgs)
                for msgs in self.pending_private_messages.values()
            )

            print("\n╭─── Connection Status ──────╮")
            print(f"│ Status: {connection_status:^18} │")
            print(f"│ Peers connected: {peer_count:6}     │")
            print(f"│ Active channels: {channel_count:6}     │")
            print(f"│ Active DMs:      {dm_count:6}     │")
            print("│                           │")
            print(f"│ Secure sessions: {session_count:6}     │")
            print(f"│ Pending handshakes: {pending_handshakes:3}     │")
            print(f"│ Queued messages: {pending_messages:6}     │")
            print("│                           │")
            print(f"│ Your nickname: {self.nickname[:11]:^11}  │")
            print(f"│ Your ID: {self.my_peer_id[:8]}...    │")
            print("╰───────────────────────────╯")

            if session_count > 0:
                print("\n🔒 Secure Sessions:")
                for peer_id in self.encryption_service.get_active_peers():
                    nickname = (
                        self.peers.get(peer_id, Peer()).nickname
                        or peer_id[:8] + "..."
                    )
                    fingerprint = self.encryption_service.get_peer_fingerprint(
                        peer_id
                    )
                    print(
                        f"  • {nickname} "
                        f"({fingerprint[:8] if fingerprint else 'Unknown'}...)"
                    )

            if pending_handshakes > 0:
                print("\n🤝 Pending Handshakes:")
                for peer_id in self.encryption_service.handshake_states.keys():
                    nickname = (
                        self.peers.get(peer_id, Peer()).nickname
                        or peer_id[:8] + "..."
                    )
                    print(f"  • {nickname}")

            if pending_messages > 0:
                print("\n📝 Queued Messages:")
                for peer_id, messages in (
                    self.pending_private_messages.items()
                ):
                    nickname = (
                        self.peers.get(peer_id, Peer()).nickname
                        or peer_id[:8] + "..."
                    )
                    print(
                        f"  • {len(messages)} message(s) for {nickname}"
                    )

            print("> ", end='', flush=True)
            return

        if line == "/clear":
            clear_screen()
            print_banner()
            print("> ", end='', flush=True)
            return

        if line.startswith("/dm "):
            await self.handle_dm_command(line)
            return

        if line == "/reply":
            if self.chat_context.last_private_sender:
                peer_id, nickname = self.chat_context.last_private_sender
                self.chat_context.enter_dm_mode(nickname, peer_id)
                debug_println(self.chat_context.get_status_line())
            else:
                print("» No private messages received yet.")
            return

        if line.startswith("/block"):
            await self.handle_block_command(line)
            return

        if line.startswith("/unblock "):
            await self.handle_unblock_command(line)
            return

        if line == "/leave":
            await self.handle_leave_command()
            return

        if line.startswith("/pass "):
            await self.handle_pass_command(line)
            return

        if line.startswith("/transfer "):
            await self.handle_transfer_command(line)
            return

        if line.startswith("/"):
            cmd = line.split()
            print(f"\033[93m⚠ Unknown command: {cmd[0]}\033[0m")
            print("\033[90mType /help to see available commands.\033[0m")
            return

        # -----------------------------------------------------------------
        # Send message in current context
        # -----------------------------------------------------------------
        if isinstance(self.chat_context.current_mode, PrivateDM):
            await self.send_private_message(
                line,
                self.chat_context.current_mode.peer_id,
                self.chat_context.current_mode.nickname,
            )
        else:
            if not self.client or not self.client.is_connected:
                print(
                    "\033[93m⚠ You're not connected to any peers yet.\033[0m"
                )
                print(
                    "\033[90mYour message will be sent once someone "
                    "joins the network.\033[0m"
                )
                print(
                    "\033[90m(This Python client doesn't queue messages "
                    "while offline)\033[0m"
                )
            else:
                await self.send_public_message(line)

    # ------------------------------------------------------------------
    # handle_join_channel
    # ------------------------------------------------------------------
    async def handle_join_channel(self, line: str):
        parts = line.split()
        if len(parts) < 2:
            print("\033[93m⚠ Usage: /j #<channel> [password]\033[0m")
            return

        channel_name = parts[1]
        password     = parts[2] if len(parts) > 2 else None

        if not channel_name.startswith("#"):
            print("\033[93m⚠ Channel names must start with #\033[0m")
            print(f"\033[90mExample: /j #{channel_name}\033[0m")
            return

        if len(channel_name) > 25:
            print("\033[93m⚠ Channel name too long (max 25 characters)\033[0m")
            return

        if not all(c.isalnum() or c in '-_' for c in channel_name[1:]):
            print("\033[93m⚠ Channel name contains invalid characters\033[0m")
            print(
                "\033[90mChannel names can only contain letters, numbers, "
                "hyphens and underscores.\033[0m"
            )
            return

        if channel_name in self.password_protected_channels and not password:
            stored_key = self.channel_keys.get(channel_name)
            if not stored_key:
                password = await aioconsole.ainput(
                    f"🔒 {channel_name} is password protected. "
                    f"Enter password: "
                )
                if not password:
                    print(
                        "\033[93m⚠ Password required to join "
                        "this channel.\033[0m"
                    )
                    return

        if password:
            key        = derive_channel_key(password, channel_name)
            self.channel_keys[channel_name] = key
            self.password_protected_channels.add(channel_name)
            commitment = compute_key_commitment(key)
            self.channel_key_commitments[channel_name] = commitment
            await self.send_channel_announce(channel_name, True, commitment)

        self.chat_context.join_channel(channel_name)
        self.chat_context.enter_channel_mode(channel_name)
        debug_println(self.chat_context.get_status_line())

        join_packet = create_bitchat_packet(
            self.my_peer_id,
            MessageType.JOIN,
            channel_name.encode(),
        )
        await self.send_packet(join_packet)
        await self.save_app_state()

    # ------------------------------------------------------------------
    # handle_dm_command
    # ------------------------------------------------------------------
    async def handle_dm_command(self, line: str):
        parts = line.split(None, 2)
        if len(parts) < 2:
            print("\033[93m⚠ Usage: /dm <nickname> [message]\033[0m")
            return

        target_nick = parts[1].strip()

        # Find peer by nickname (case-insensitive)
        target_peer_id = None
        for peer_id, peer in self.peers.items():
            if peer.nickname and peer.nickname.lower() == target_nick.lower():
                target_peer_id = peer_id
                break

        if not target_peer_id:
            print(f"\033[93m⚠ User '{target_nick}' not found.\033[0m")
            print("\033[90mUse /online to see who's connected.\033[0m")
            return

        self.chat_context.enter_dm_mode(target_nick, target_peer_id)
        debug_println(self.chat_context.get_status_line())

        # If an inline message was supplied, send it immediately
        if len(parts) == 3:
            inline_message = parts[2].strip()
            if inline_message:
                await self.send_private_message(
                    inline_message, target_peer_id, target_nick
                )
                return

        if not self.encryption_service.is_session_established(target_peer_id):
            print(
                f"\033[90m» Initiating secure session with "
                f"{target_nick}...\033[0m"
            )
            try:
                handshake_message = self.encryption_service.initiate_handshake(
                    target_peer_id
                )
                handshake_packet = create_bitchat_packet_with_recipient(
                    self.my_peer_id,
                    target_peer_id,
                    MessageType.NOISE_HANDSHAKE_INIT,
                    handshake_message,
                    None,
                )
                hs_data      = bytearray(handshake_packet)
                hs_data[2]  = 3
                await self.send_packet(bytes(hs_data))
            except Exception as e:
                debug_println(
                    f"[NOISE] Failed to initiate handshake for DM: {e}"
                )

    # ------------------------------------------------------------------
    # handle_block_command
    # ------------------------------------------------------------------
    async def handle_block_command(self, line: str):
        parts = line.split()
        if len(parts) < 2:
            if self.blocked_peers:
                print("» Blocked peers:")
                for peer_id in self.blocked_peers:
                    nickname = (
                        self.peers.get(peer_id, Peer()).nickname
                        or peer_id[:8] + "..."
                    )
                    print(f"  • {nickname} ({peer_id[:8]}...)")
            else:
                print("» No blocked peers.")
            return

        target_nick    = parts[1]
        target_peer_id = None
        for peer_id, peer in self.peers.items():
            if peer.nickname and peer.nickname.lower() == target_nick.lower():
                target_peer_id = peer_id
                break

        if not target_peer_id:
            print(f"\033[93m⚠ User '{target_nick}' not found.\033[0m")
            return

        self.blocked_peers.add(target_peer_id)
        print(f"\033[90m» {target_nick} has been blocked.\033[0m")
        await self.save_app_state()

    # ------------------------------------------------------------------
    # handle_unblock_command
    # ------------------------------------------------------------------
    async def handle_unblock_command(self, line: str):
        parts = line.split()
        if len(parts) < 2:
            print("\033[93m⚠ Usage: /unblock <nickname>\033[0m")
            return

        target_nick    = parts[1]
        target_peer_id = None

        for peer_id, peer in self.peers.items():
            if peer.nickname and peer.nickname.lower() == target_nick.lower():
                target_peer_id = peer_id
                break

        if not target_peer_id:
            for peer_id in self.blocked_peers:
                if peer_id.startswith(target_nick):
                    target_peer_id = peer_id
                    break

        if not target_peer_id:
            print(
                f"\033[93m⚠ User '{target_nick}' not found "
                f"in blocked list.\033[0m"
            )
            return

        self.blocked_peers.discard(target_peer_id)
        print(f"\033[90m» {target_nick} has been unblocked.\033[0m")
        await self.save_app_state()

    # ------------------------------------------------------------------
    # handle_leave_command
    # ------------------------------------------------------------------
    async def handle_leave_command(self):
        if isinstance(self.chat_context.current_mode, Channel):
            channel     = self.chat_context.current_mode.name
            leave_packet = create_bitchat_packet(
                self.my_peer_id,
                MessageType.LEAVE,
                channel.encode(),
            )
            await self.send_packet(leave_packet)

            self.chat_context.leave_channel(channel)
            self.chat_context.switch_to_public()
            debug_println(self.chat_context.get_status_line())

            self.channel_keys.pop(channel, None)
            self.channel_key_commitments.pop(channel, None)
            self.password_protected_channels.discard(channel)

            await self.save_app_state()
            print(f"\033[90m» Left {channel}\033[0m")

        elif isinstance(self.chat_context.current_mode, PrivateDM):
            nickname = self.chat_context.current_mode.nickname
            self.chat_context.switch_to_public()
            debug_println(self.chat_context.get_status_line())
            print(f"\033[90m» Left DM with {nickname}\033[0m")

        else:
            print("\033[90m» You're already in public chat.\033[0m")

    # ------------------------------------------------------------------
    # handle_pass_command
    # ------------------------------------------------------------------
    async def handle_pass_command(self, line: str):
        parts = line.split(None, 2)
        if len(parts) < 3:
            print("\033[93m⚠ Usage: /pass <#channel> <password>\033[0m")
            return

        channel  = parts[1]
        password = parts[2]

        if not channel.startswith('#'):
            print("\033[93m⚠ Channel name must start with #\033[0m")
            return

        key        = derive_channel_key(password, channel)
        self.channel_keys[channel] = key
        self.password_protected_channels.add(channel)
        commitment = compute_key_commitment(key)
        self.channel_key_commitments[channel] = commitment

        print(f"\033[90m» Password set for {channel}\033[0m")
        await self.send_channel_announce(channel, True, commitment)
        await self.save_app_state()

    # ------------------------------------------------------------------
    # handle_transfer_command
    # ------------------------------------------------------------------
    async def handle_transfer_command(self, line: str):
        parts = line.split(None, 2)
        if len(parts) < 3:
            print(
                "\033[93m⚠ Usage: /transfer <#channel> "
                "<new_owner_nickname>\033[0m"
            )
            return

        channel       = parts[1]
        new_owner_nick = parts[2]

        if not channel.startswith('#'):
            print("\033[93m⚠ Channel name must start with #\033[0m")
            return

        current_owner = self.channel_creators.get(channel)
        if current_owner != self.my_peer_id:
            print(
                f"\033[93m⚠ You are not the owner of {channel}\033[0m"
            )
            return

        target_peer_id = None
        for peer_id, peer in self.peers.items():
            if (
                peer.nickname
                and peer.nickname.lower() == new_owner_nick.lower()
            ):
                target_peer_id = peer_id
                break

        if not target_peer_id:
            print(
                f"\033[93m⚠ User '{new_owner_nick}' not found.\033[0m"
            )
            return

        self.channel_creators[channel] = target_peer_id
        await self.send_channel_announce(
            channel,
            channel in self.password_protected_channels,
            self.channel_key_commitments.get(channel),
        )
        print(
            f"\033[90m» Ownership of {channel} transferred to "
            f"{new_owner_nick}\033[0m"
        )
        await self.save_app_state()

    # ------------------------------------------------------------------
    # send_public_message
    # ------------------------------------------------------------------
    async def send_public_message(self, text: str):
        channel_key  = None
        channel_name = None

        if isinstance(self.chat_context.current_mode, Channel):
            channel_name = self.chat_context.current_mode.name
            channel_key  = self.channel_keys.get(channel_name)

        msg_id    = str(uuid.uuid4())
        timestamp = int(time.time() * 1000)

        # Current BitChat public messages carry the RAW UTF-8 content as the
        # payload (identity + timestamp live in the packet header), not the
        # legacy BitchatMessage struct.
        payload = text.encode("utf-8")

        packet = create_signed_bitchat_packet(
            self.my_peer_id,
            MessageType.MESSAGE,
            payload,
            self.encryption_service.sign_data,
            ttl=5,
        )
        await self.send_packet(packet)

        self.delivery_tracker.track_message(msg_id, text, False)

        if channel_name:
            print(
                f"\r\033[K\033[32m[{channel_name}] "
                f"{self.nickname}\033[0m: {text}\n> ",
                end='', flush=True,
            )
        else:
            print(
                f"\r\033[K\033[32m{self.nickname}\033[0m: {text}\n> ",
                end='', flush=True,
            )

    # ------------------------------------------------------------------
    # send_private_message
    # ------------------------------------------------------------------
    async def send_private_message(
        self,
        text:       str,
        peer_id:    str,
        nickname:   str,
        message_id: Optional[str] = None,
    ):
        msg_id = message_id or str(uuid.uuid4())

        if not self.encryption_service.is_session_established(peer_id):
            # Queue the full tuple so send_pending_private_messages can
            # replay it correctly once the handshake completes.
            if peer_id not in self.pending_private_messages:
                self.pending_private_messages[peer_id] = []
            self.pending_private_messages[peer_id].append(
                (text, nickname, msg_id)
            )

            if peer_id not in self.encryption_service.handshake_states:
                try:
                    handshake_message = (
                        self.encryption_service.initiate_handshake(peer_id)
                    )
                    handshake_packet = create_bitchat_packet_with_recipient(
                        self.my_peer_id,
                        peer_id,
                        MessageType.NOISE_HANDSHAKE_INIT,
                        handshake_message,
                        None,
                    )
                    hs_data      = bytearray(handshake_packet)
                    hs_data[2]  = 3
                    await self.send_packet(bytes(hs_data))
                    print(
                        f"\r\033[K\033[90m» Establishing secure session with "
                        f"{nickname}... message queued.\033[0m\n> ",
                        end='', flush=True,
                    )
                except Exception as e:
                    debug_println(
                        f"[NOISE] Failed to initiate handshake: {e}"
                    )
            else:
                print(
                    f"\r\033[K\033[90m» Handshake in progress with "
                    f"{nickname}... message queued.\033[0m\n> ",
                    end='', flush=True,
                )
            return

        timestamp = int(time.time() * 1000)

        # Current BitChat private message: encrypt
        #   [NoisePayloadType.privateMessage=0x01] + PrivateMessagePacket TLV
        #   (0x00 messageID, 0x01 content)   [each TLV length is 1 byte]
        # then wrap the ciphertext in a SIGNED NOISE_ENCRYPTED (0x11) packet
        # addressed to the peer (the phones sign these too -> flags 0x03).
        _mid   = msg_id.encode('utf-8')[:255]
        _cont  = text.encode('utf-8')[:255]
        pm_tlv = (bytes([0x00, len(_mid)]) + _mid
                  + bytes([0x01, len(_cont)]) + _cont)
        plaintext = bytes([0x01]) + pm_tlv         # 0x01 = privateMessage

        encrypted_payload = self.encryption_service.encrypt_for_peer(
            peer_id, plaintext
        )

        outer_packet = create_signed_bitchat_packet(
            self.my_peer_id,
            MessageType.NOISE_ENCRYPTED,
            encrypted_payload,
            self.encryption_service.sign_data,
            ttl=3,
            recipient_id=peer_id,
        )
        await self.send_packet(outer_packet)

        self.delivery_tracker.track_message(msg_id, text, True)
        print(
            f"\r\033[K\033[35m🔒 {self.nickname} → {nickname}\033[0m: "
            f"{text}\n> ",
            end='', flush=True,
        )

    # ------------------------------------------------------------------
    # run_input_loop
    # ------------------------------------------------------------------
    async def run_input_loop(self):
        print("> ", end='', flush=True)
        while self.running:
            try:
                line = await aioconsole.ainput("")
                line = line.strip()
                if line:
                    await self.handle_user_input(line)
                if self.running:
                    print("> ", end='', flush=True)
            except (EOFError, KeyboardInterrupt):
                await self.handle_user_input("/exit")
                break
            except Exception as e:
                debug_println(f"[INPUT] Error: {e}")

    # ------------------------------------------------------------------
    # run — main entry point
    # ------------------------------------------------------------------
    async def run(self):
        clear_screen()
        print_banner()

        # Load persisted state before anything else
        self.app_state = load_state()
        if self.app_state.nickname:
            self.nickname = self.app_state.nickname
        if self.app_state.blocked_peers:
            self.blocked_peers = self.app_state.blocked_peers
        if self.app_state.channel_creators:
            self.channel_creators = self.app_state.channel_creators
        if self.app_state.joined_channels:
            for ch in self.app_state.joined_channels:
                self.chat_context.join_channel(ch)
        if self.app_state.password_protected_channels:
            self.password_protected_channels = (
                self.app_state.password_protected_channels
            )
        if self.app_state.channel_key_commitments:
            self.channel_key_commitments = (
                self.app_state.channel_key_commitments
            )

        print(
            f"\033[90m» Your nickname: \033[97m{self.nickname}\033[0m"
        )
        print(
            f"\033[90m» Your peer ID:  \033[97m{self.my_peer_id}\033[0m"
        )
        print(
            "\033[90m» Scanning for BitChat peers via BLE...\033[0m\n"
        )

        # Step 1 — initial BLE scan + connect attempt
        connected = await self.connect()

        if connected and self.client and self.client.is_connected:
            debug_println("[RUN] BLE connected on first attempt")
        else:
            debug_println(
                "[RUN] No BLE peer found on first scan — offline mode"
            )

        # Step 2 — handshake AFTER connect
        await self.handshake()

        # Step 3 — run input loop and background scanner concurrently
        await asyncio.gather(
            self.run_input_loop(),
            self.background_scanner(),
        )

        debug_println("[RUN] Exiting.")


# ---------------------------------------------------------------------------
# Module-level entry point
# ---------------------------------------------------------------------------
async def main():
    client = BitchatClient()
    try:
        await client.run()
    except KeyboardInterrupt:
        pass
    finally:
        if client.client and client.client.is_connected:
            try:
                leave_packet = create_bitchat_packet(
                    client.my_peer_id,
                    MessageType.LEAVE,
                    client.nickname.encode(),
                )
                await client.send_packet(leave_packet)
                await asyncio.sleep(0.1)
                await client.client.disconnect()
            except Exception:
                pass
        await client.save_app_state()


if __name__ == "__main__":
    asyncio.run(main())
