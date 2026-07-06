from typing import Tuple

import zlib

COMPRESSION_THRESHOLD = 100


def compress_if_beneficial(data: bytes) -> Tuple[bytes, bool]:
    """Current BitChat (permissionlesstech) compresses payloads with Apple's
    COMPRESSION_ZLIB, i.e. *raw* DEFLATE (RFC-1951, no zlib header). Rather than
    risk a header/format mismatch, Sage never compresses her own outbound
    packets — they are small and fit inside one BLE MTU. Inbound compressed
    packets are still handled by decompress()."""
    return (data, False)


def decompress(data: bytes) -> bytes:
    """Decompress an inbound compressed payload. Current BitChat uses raw
    DEFLATE (wbits=-15). We also try a zlib-wrapped stream and gzip, then fall
    back to legacy LZ4, before giving up."""
    raw = bytes(data)
    for wbits in (-15, 15, 47):
        try:
            return zlib.decompressobj(wbits).decompress(raw)
        except Exception:
            continue
    try:
        import lz4.frame
        return lz4.frame.decompress(raw)
    except Exception as e:
        raise ValueError(f"Decompression failed: {e}")


# Export functions
__all__ = ['compress_if_beneficial', 'decompress', 'COMPRESSION_THRESHOLD']
