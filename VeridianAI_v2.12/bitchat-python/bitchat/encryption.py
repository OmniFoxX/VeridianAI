"""
Encryption Service for BitChat
Implements both Noise Protocol (XX pattern) and legacy encryption layers.
Compatible with Swift NoiseEncryptionService implementation.
"""

import os
import time
import json
import secrets
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, Callable
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hmac import HMAC
import hashlib


# Noise Protocol Constants
NOISE_PROTOCOL_NAME = "Noise_XX_25519_ChaChaPoly_SHA256"
NOISE_DH_LEN = 32  # Curve25519 key size
NOISE_HASH_LEN = 32  # SHA256 hash size


class NoiseError(Exception):
    """Base class for Noise protocol errors"""
    pass


class NoiseRole:
    """Noise handshake roles"""
    INITIATOR = "initiator"
    RESPONDER = "responder"


class NoiseHandshakeState:
    """
    Noise handshake state machine for XX pattern.
    Implements the Noise Protocol Framework specification.
    """

    def __init__(self, role: str, local_static_key: X25519PrivateKey, remote_static_key: Optional[X25519PublicKey] = None):
        self.role = role
        self.local_static_private = local_static_key
        self.local_static_public = local_static_key.public_key()
        self.remote_static_public = remote_static_key

        # Ephemeral keys
        self.local_ephemeral_private = None
        self.local_ephemeral_public = None
        self.remote_ephemeral_public = None

        # Symmetric state
        self.chaining_key = None
        self.hash_state = None
        self.cipher_state = NoiseCipherState()

        # Pattern tracking
        self.current_pattern = 0
        self.message_patterns = self._get_xx_patterns()

        # Initialize symmetric state
        self._initialize_symmetric_state()

    def _get_xx_patterns(self) -> list:
        """Get XX pattern message sequences"""
        return [
            ['e'],                    # Message 1: -> e
            ['e', 'ee', 's', 'es'],   # Message 2: <- e, ee, s, es
            ['s', 'se']               # Message 3: -> s, se
        ]

    def _initialize_symmetric_state(self):
        """Initialize symmetric state with protocol name"""
        protocol_name = NOISE_PROTOCOL_NAME.encode('utf-8')
        if len(protocol_name) <= 32:
            self.hash_state = protocol_name + b'\x00' * (32 - len(protocol_name))
        else:
            self.hash_state = hashlib.sha256(protocol_name).digest()
        self.chaining_key = self.hash_state

        # Mix the (empty) prologue, exactly as iOS does in mixPreMessageKeys().
        # Without this, our handshake hash stays one SHA-256 behind iOS and every
        # encrypted handshake field fails the peer's AEAD auth -> session never
        # completes. (Noise spec: InitializeSymmetric then MixHash(prologue).)
        self._mix_hash(b'')

    def _mix_key(self, input_key_material: bytes):
        """Mix key material into chaining key and update cipher"""
        # HKDF extract: tempKey = HMAC(chainingKey, inputKeyMaterial)
        hmac = HMAC(self.chaining_key, hashes.SHA256())
        hmac.update(input_key_material)
        temp_key = hmac.finalize()

        # HKDF expand: generate 2 outputs (matching Swift)
        hmac1 = HMAC(temp_key, hashes.SHA256())
        hmac1.update(b'\x01')
        output1 = hmac1.finalize()

        hmac2 = HMAC(temp_key, hashes.SHA256())
        hmac2.update(output1 + b'\x02')
        output2 = hmac2.finalize()

        self.chaining_key = output1
        self.cipher_state.initialize_key(output2)

    def _mix_hash(self, data: bytes):
        """Mix data into handshake hash"""
        digest = hashes.Hash(hashes.SHA256())
        digest.update(self.hash_state)
        digest.update(data)
        self.hash_state = digest.finalize()

    def _mix_key_and_hash(self, input_key_material: bytes):
        """Mix key material into both chaining key and hash"""
        # HKDF extract: tempKey = HMAC(chainingKey, inputKeyMaterial)
        hmac = HMAC(self.chaining_key, hashes.SHA256())
        hmac.update(input_key_material)
        temp_key = hmac.finalize()

        # HKDF expand: generate 3 outputs (matching Swift)
        hmac1 = HMAC(temp_key, hashes.SHA256())
        hmac1.update(b'\x01')
        output1 = hmac1.finalize()

        hmac2 = HMAC(temp_key, hashes.SHA256())
        hmac2.update(output1 + b'\x02')
        output2 = hmac2.finalize()

        hmac3 = HMAC(temp_key, hashes.SHA256())
        hmac3.update(output2 + b'\x03')
        output3 = hmac3.finalize()

        self.chaining_key = output1
        self._mix_hash(output2)
        self.cipher_state.initialize_key(output3)

    def _encrypt_and_hash(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext and mix ciphertext into hash"""
        if self.cipher_state.has_key():
            ciphertext = self.cipher_state.encrypt(plaintext, self.hash_state)
            self._mix_hash(ciphertext)
            return ciphertext
        else:
            self._mix_hash(plaintext)
            return plaintext

    def _decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        """Decrypt ciphertext and mix it into hash"""
        if self.cipher_state.has_key():
            plaintext = self.cipher_state.decrypt(ciphertext, self.hash_state)
            self._mix_hash(ciphertext)
            return plaintext
        else:
            self._mix_hash(ciphertext)
            return ciphertext

    def _dh(self, private_key: X25519PrivateKey, public_key: X25519PublicKey) -> bytes:
        """Perform Diffie-Hellman key exchange"""
        return private_key.exchange(public_key)

    def write_message(self, payload: bytes = b'') -> bytes:
        """Write a handshake message"""
        if self.current_pattern >= len(self.message_patterns):
            raise NoiseError("Handshake complete")

        message_buffer = bytearray()
        patterns = self.message_patterns[self.current_pattern]

        for pattern in patterns:
            if pattern == 'e':
                # Generate and send ephemeral key
                self.local_ephemeral_private = X25519PrivateKey.generate()
                self.local_ephemeral_public = self.local_ephemeral_private.public_key()
                ephemeral_bytes = self.local_ephemeral_public.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw
                )
                message_buffer.extend(ephemeral_bytes)
                self._mix_hash(ephemeral_bytes)

            elif pattern == 's':
                # Send static key (encrypted if cipher is initialized)
                static_bytes = self.local_static_public.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw
                )
                encrypted = self._encrypt_and_hash(static_bytes)
                message_buffer.extend(encrypted)

            elif pattern == 'ee':
                if not self.local_ephemeral_private or not self.remote_ephemeral_public:
                    raise NoiseError("Missing ephemeral keys for ee")
                shared = self._dh(self.local_ephemeral_private, self.remote_ephemeral_public)
                self._mix_key(shared)

            elif pattern == 'es':
                if self.role == NoiseRole.INITIATOR:
                    if not self.local_ephemeral_private or not self.remote_static_public:
                        raise NoiseError("Missing keys for es")
                    shared = self._dh(self.local_ephemeral_private, self.remote_static_public)
                else:
                    if not self.local_static_private or not self.remote_ephemeral_public:
                        raise NoiseError("Missing keys for es")
                    shared = self._dh(self.local_static_private, self.remote_ephemeral_public)
                self._mix_key(shared)

            elif pattern == 'se':
                if self.role == NoiseRole.INITIATOR:
                    if not self.local_static_private or not self.remote_ephemeral_public:
                        raise NoiseError("Missing keys for se")
                    shared = self._dh(self.local_static_private, self.remote_ephemeral_public)
                else:
                    if not self.local_ephemeral_private or not self.remote_static_public:
                        raise NoiseError("Missing keys for se")
                    shared = self._dh(self.local_ephemeral_private, self.remote_static_public)
                self._mix_key(shared)

        # Encrypt payload
        encrypted_payload = self._encrypt_and_hash(payload)
        message_buffer.extend(encrypted_payload)

        self.current_pattern += 1
        return bytes(message_buffer)

    def read_message(self, message: bytes) -> bytes:
        """Read a handshake message"""
        if self.current_pattern >= len(self.message_patterns):
            raise NoiseError("Handshake complete")

        buffer = message
        patterns = self.message_patterns[self.current_pattern]

        for pattern in patterns:
            if pattern == 'e':
                if len(buffer) < 32:
                    raise NoiseError("Invalid message: insufficient data for ephemeral key")
                ephemeral_data = buffer[:32]
                buffer = buffer[32:]
                self.remote_ephemeral_public = X25519PublicKey.from_public_bytes(ephemeral_data)
                self._mix_hash(ephemeral_data)

            elif pattern == 's':
                key_length = 48 if self.cipher_state.has_key() else 32
                if len(buffer) < key_length:
                    raise NoiseError("Invalid message: insufficient data for static key")
                static_data = buffer[:key_length]
                buffer = buffer[key_length:]
                decrypted = self._decrypt_and_hash(static_data)
                self.remote_static_public = X25519PublicKey.from_public_bytes(decrypted)

            elif pattern in ['ee', 'es', 'se']:
                if pattern == 'ee':
                    if not self.local_ephemeral_private or not self.remote_ephemeral_public:
                        raise NoiseError("Missing ephemeral keys for ee")
                    shared = self._dh(self.local_ephemeral_private, self.remote_ephemeral_public)
                    self._mix_key(shared)
                elif pattern == 'es':
                    if self.role == NoiseRole.INITIATOR:
                        if not self.local_ephemeral_private or not self.remote_static_public:
                            raise NoiseError("Missing keys for es")
                        shared = self._dh(self.local_ephemeral_private, self.remote_static_public)
                    else:
                        if not self.local_static_private or not self.remote_ephemeral_public:
                            raise NoiseError("Missing keys for es")
                        shared = self._dh(self.local_static_private, self.remote_ephemeral_public)
                    self._mix_key(shared)
                elif pattern == 'se':
                    if self.role == NoiseRole.INITIATOR:
                        if not self.local_static_private or not self.remote_ephemeral_public:
                            raise NoiseError("Missing keys for se")
                        shared = self._dh(self.local_static_private, self.remote_ephemeral_public)
                    else:
                        if not self.local_ephemeral_private or not self.remote_static_public:
                            raise NoiseError("Missing keys for se")
                        shared = self._dh(self.local_ephemeral_private, self.remote_static_public)
                    self._mix_key(shared)

        # Decrypt payload
        payload = self._decrypt_and_hash(buffer)
        self.current_pattern += 1
        return payload

    def is_handshake_complete(self) -> bool:
        """Check if handshake is complete"""
        return self.current_pattern >= len(self.message_patterns)

    def get_transport_ciphers(self) -> Tuple['NoiseCipherState', 'NoiseCipherState']:
        """Get transport cipher states after handshake completion"""
        if not self.is_handshake_complete():
            raise NoiseError("Handshake not complete")

        # Split function: derive two cipher states (matching Swift)
        hmac = HMAC(self.chaining_key, hashes.SHA256())
        hmac.update(b'')
        temp_key = hmac.finalize()

        hmac1 = HMAC(temp_key, hashes.SHA256())
        hmac1.update(b'\x01')
        key1 = hmac1.finalize()

        hmac2 = HMAC(temp_key, hashes.SHA256())
        hmac2.update(key1 + b'\x02')
        key2 = hmac2.finalize()

        c1 = NoiseCipherState(use_extracted_nonce=True)
        c1.initialize_key(key1)

        c2 = NoiseCipherState(use_extracted_nonce=True)
        c2.initialize_key(key2)

        # Initiator uses c1 for sending, c2 for receiving
        # Responder uses c2 for sending, c1 for receiving
        if self.role == NoiseRole.INITIATOR:
            return c1, c2
        else:
            return c2, c1

    def get_handshake_hash(self) -> bytes:
        """Get the handshake hash for channel binding"""
        return self.hash_state

    def get_remote_static_public_key(self) -> Optional[X25519PublicKey]:
        """Get the remote static public key"""
        return self.remote_static_public


class NoiseCipherState:
    """Cipher state for Noise Protocol transport encryption"""

    def __init__(self, use_extracted_nonce: bool = False):
        self.key = None
        self.nonce = 0
        # Transport ciphers (post-handshake) prepend a 4-byte big-endian nonce
        # to each frame, matching iOS getTransportCiphers(useExtractedNonce:true).
        self.use_extracted_nonce = use_extracted_nonce

    def initialize_key(self, key: bytes):
        """Initialize cipher with key"""
        self.key = key
        self.nonce = 0

    def has_key(self) -> bool:
        """Check if cipher has a key"""
        return self.key is not None

    def encrypt(self, plaintext: bytes, associated_data: bytes = b'') -> bytes:
        """Encrypt plaintext with ChaCha20-Poly1305"""
        if not self.has_key():
            raise NoiseError("Cipher not initialized")

        counter = self.nonce
        # AEAD nonce: 4 zero bytes + 8-byte little-endian counter (matching Swift)
        nonce = b'\x00\x00\x00\x00' + counter.to_bytes(8, byteorder='little')

        cipher = ChaCha20Poly1305(self.key)
        ciphertext = cipher.encrypt(nonce, plaintext, associated_data)

        self.nonce += 1
        if self.use_extracted_nonce:
            # Transport frame: <4-byte big-endian nonce><ciphertext+tag>
            return counter.to_bytes(4, byteorder='big') + ciphertext
        return ciphertext

    def decrypt(self, ciphertext: bytes, associated_data: bytes = b'') -> bytes:
        """Decrypt ciphertext with ChaCha20-Poly1305"""
        if not self.has_key():
            raise NoiseError("Cipher not initialized")

        if self.use_extracted_nonce:
            # Transport frame: <4-byte big-endian nonce><ciphertext+tag>
            if len(ciphertext) < 4:
                raise NoiseError("Ciphertext too short for extracted nonce")
            counter = int.from_bytes(ciphertext[:4], byteorder='big')
            body    = ciphertext[4:]
            nonce   = b'\x00\x00\x00\x00' + counter.to_bytes(8, byteorder='little')
            return ChaCha20Poly1305(self.key).decrypt(nonce, body, associated_data)

        # Nonce: 4 zero bytes + 8-byte little-endian counter (matching Swift)
        nonce = b'\x00\x00\x00\x00' + self.nonce.to_bytes(8, byteorder='little')
        cipher = ChaCha20Poly1305(self.key)
        try:
            plaintext = cipher.decrypt(nonce, ciphertext, associated_data)
            self.nonce += 1
            return plaintext
        except Exception as e:
            # Increment nonce even on failure to maintain sync (Noise protocol requirement)
            self.nonce += 1
            raise


@dataclass
class NoiseSession:
    """Represents an established Noise session with a peer"""
    peer_id: str
    send_cipher: NoiseCipherState
    receive_cipher: NoiseCipherState
    remote_static_key: X25519PublicKey
    established_time: float

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt data for transport"""
        return self.send_cipher.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt received data"""
        return self.receive_cipher.decrypt(ciphertext)

    def get_fingerprint(self) -> str:
        """Get peer's public key fingerprint"""
        key_bytes = self.remote_static_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        return hashlib.sha256(key_bytes).hexdigest()


class EncryptionService:
    """
    Main encryption service implementing both Noise Protocol and legacy encryption.
    Compatible with Swift NoiseEncryptionService.
    """

    def __init__(self, identity_path: Optional[str] = None):
        # X25519 static key for Noise protocol key exchange
        self.static_identity_key = self._load_or_create_identity(identity_path)

        # Ed25519 signing key — separate from X25519, produces proper 64-byte signatures
        # iOS expects Ed25519 for identity announcement signing, not SHA256 hashes
        self.signing_key = self._load_or_create_signing_key(identity_path)

        # Active Noise sessions
        self.sessions: Dict[str, NoiseSession] = {}

        # Handshake states in progress
        self.handshake_states: Dict[str, NoiseHandshakeState] = {}

        # Store our peer ID for tie-breaking (set from outside)
        self.my_peer_id: Optional[str] = None

        # Callbacks
        self.on_peer_authenticated: Optional[Callable[[str, str], None]] = None
        self.on_handshake_required: Optional[Callable[[str], None]] = None

    def _load_or_create_identity(self, identity_path: Optional[str]) -> X25519PrivateKey:
        """Load existing X25519 identity or create new one"""
        if identity_path and os.path.exists(identity_path):
            try:
                with open(identity_path, 'rb') as f:
                    key_data = f.read()
                return X25519PrivateKey.from_private_bytes(key_data)
            except Exception:
                pass

        # Create new X25519 identity key
        key = X25519PrivateKey.generate()

        if identity_path:
            try:
                os.makedirs(os.path.dirname(identity_path), exist_ok=True)
                with open(identity_path, 'wb') as f:
                    f.write(key.private_bytes(
                        encoding=serialization.Encoding.Raw,
                        format=serialization.PrivateFormat.Raw,
                        encryption_algorithm=serialization.NoEncryption()
                    ))
                os.chmod(identity_path, 0o600)
            except Exception:
                pass

        return key

    def _load_or_create_signing_key(self, identity_path: Optional[str]) -> Ed25519PrivateKey:
        """
        Load existing Ed25519 signing key or create new one.
        Stored alongside the X25519 identity key with a _signing suffix.
        This is a completely separate keypair from the X25519 encryption key —
        Ed25519 is for signing, X25519 is for Diffie-Hellman key exchange.
        iOS expects a real Ed25519 public key and 64-byte Ed25519 signatures.
        """
        signing_path = None
        if identity_path:
            # Store as identity_signing.key alongside identity.key
            signing_path = identity_path.replace('.key', '_signing.key')
            if signing_path == identity_path:
                # No .key extension — just append _signing
                signing_path = identity_path + '_signing'

        if signing_path and os.path.exists(signing_path):
            try:
                with open(signing_path, 'rb') as f:
                    key_data = f.read()
                return Ed25519PrivateKey.from_private_bytes(key_data)
            except Exception:
                pass

        # Create new Ed25519 signing key
        key = Ed25519PrivateKey.generate()

        if signing_path:
            try:
                os.makedirs(os.path.dirname(signing_path), exist_ok=True)
                with open(signing_path, 'wb') as f:
                    f.write(key.private_bytes(
                        encoding=serialization.Encoding.Raw,
                        format=serialization.PrivateFormat.Raw,
                        encryption_algorithm=serialization.NoEncryption()
                    ))
                os.chmod(signing_path, 0o600)
            except Exception:
                pass

        return key

    def get_identity_fingerprint(self) -> str:
        """Get our X25519 identity fingerprint"""
        public_key = self.static_identity_key.public_key()
        key_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        return hashlib.sha256(key_bytes).hexdigest()

    def get_public_key_bytes(self) -> bytes:
        """Get our X25519 public key bytes for Noise key exchange"""
        public_key = self.static_identity_key.public_key()
        return public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )

    def get_public_key(self) -> bytes:
        """Get X25519 public key bytes (alias for compatibility)"""
        return self.get_public_key_bytes()

    def get_combined_public_key_data(self) -> bytes:
        """Get combined public key data for legacy compatibility"""
        return self.get_public_key_bytes()

    def get_signing_public_key_bytes(self) -> bytes:
        """
        Get Ed25519 signing public key bytes.
        This is distinct from the X25519 encryption key.
        iOS expects a real Ed25519 public key here — 32 bytes, correct curve.
        """
        public_key = self.signing_key.public_key()
        return public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )

    def initiate_handshake(self, peer_id: str) -> bytes:
        """Initiate Noise XX handshake with a peer"""
        # Clean up any existing handshake state and session
        if peer_id in self.handshake_states:
            del self.handshake_states[peer_id]

        if peer_id in self.sessions:
            del self.sessions[peer_id]

        # Create new handshake state as initiator
        handshake = NoiseHandshakeState(NoiseRole.INITIATOR, self.static_identity_key)
        self.handshake_states[peer_id] = handshake

        # Write first message (-> e)
        return handshake.write_message()

    def process_handshake_message(self, peer_id: str, message: bytes) -> Optional[bytes]:
        """Process incoming handshake message and return response if needed"""

        if not message:
            raise NoiseError("Empty handshake message")

        if len(message) < 32:
            raise NoiseError(f"Handshake message too short: {len(message)} bytes")

        # Check if we have an ongoing handshake
        if peer_id in self.handshake_states:
            handshake = self.handshake_states[peer_id]
        else:
            # New handshake from peer — we are responder
            handshake = NoiseHandshakeState(NoiseRole.RESPONDER, self.static_identity_key)
            self.handshake_states[peer_id] = handshake

        if handshake.current_pattern >= len(handshake.message_patterns):
            return None

        try:
            # Read the incoming message
            payload = handshake.read_message(message)

            # Check if we need to send a response
            response = None
            if not handshake.is_handshake_complete():
                response = handshake.write_message()

            # Check if handshake is now complete
            if handshake.is_handshake_complete():
                send_cipher, receive_cipher = handshake.get_transport_ciphers()

                remote_key = handshake.get_remote_static_public_key()
                if remote_key:
                    session = NoiseSession(
                        peer_id=peer_id,
                        send_cipher=send_cipher,
                        receive_cipher=receive_cipher,
                        remote_static_key=remote_key,
                        established_time=time.time()
                    )
                    self.sessions[peer_id] = session

                    # Cleanup handshake state
                    del self.handshake_states[peer_id]

                    # Notify authentication
                    if self.on_peer_authenticated:
                        fingerprint = session.get_fingerprint()
                        self.on_peer_authenticated(peer_id, fingerprint)

            return response

        except Exception as e:
            # Handshake failed — cleanup
            if peer_id in self.handshake_states:
                del self.handshake_states[peer_id]
            import traceback
            raise NoiseError(f"Handshake failed: {e}")

    def handle_handshake_message(self, peer_id: str, message: bytes) -> Optional[bytes]:
        """Legacy compatibility method — delegates to process_handshake_message"""
        return self.process_handshake_message(peer_id, message)

    def has_established_session(self, peer_id: str) -> bool:
        """Check if we have an established session with peer"""
        return peer_id in self.sessions

    def is_session_established(self, peer_id: str) -> bool:
        """Alias for has_established_session for compatibility"""
        return self.has_established_session(peer_id)

    def encrypt(self, data: bytes, peer_id: str) -> bytes:
        """Encrypt data for a specific peer"""
        if peer_id not in self.sessions:
            if self.on_handshake_required:
                self.on_handshake_required(peer_id)
            raise NoiseError(f"No session with peer {peer_id}")

        return self.sessions[peer_id].encrypt(data)

    def encrypt_for_peer(self, peer_id: str, data: bytes) -> bytes:
        """Encrypt data for a specific peer (reordered args for compatibility)"""
        return self.encrypt(data, peer_id)

    def decrypt_from_peer(self, peer_id: str, data: bytes) -> bytes:
        """Decrypt data from a specific peer"""
        if peer_id not in self.sessions:
            raise NoiseError(f"No session with peer {peer_id}")

        return self.sessions[peer_id].decrypt(data)

    def get_peer_fingerprint(self, peer_id: str) -> Optional[str]:
        """Get fingerprint for a peer's X25519 key"""
        if peer_id in self.sessions:
            return self.sessions[peer_id].get_fingerprint()
        return None

    def sign_data(self, data: bytes) -> bytes:
        """
        Sign data with Ed25519 private key.
        Produces a proper 64-byte Ed25519 signature as iOS expects.
        Previously this returned a 32-byte SHA256 hash which iOS rejected.
        """
        return self.signing_key.sign(data)

    def remove_session(self, peer_id: str):
        """Remove session and any handshake state for a peer"""
        if peer_id in self.sessions:
            del self.sessions[peer_id]
        if peer_id in self.handshake_states:
            del self.handshake_states[peer_id]

    def clear_handshake_state(self, peer_id: str):
        """Clear handshake state for a peer (used when handshake fails)"""
        if peer_id in self.handshake_states:
            del self.handshake_states[peer_id]

    def cleanup_old_sessions(self, max_age: float = 3600):
        """Remove sessions older than max_age seconds"""
        current_time = time.time()
        expired_peers = [
            peer_id for peer_id, session in self.sessions.items()
            if current_time - session.established_time > max_age
        ]
        for peer_id in expired_peers:
            del self.sessions[peer_id]

    def get_session_count(self) -> int:
        """Get number of active sessions"""
        return len(self.sessions)

    def get_active_peers(self) -> list:
        """Get list of peers with active sessions"""
        return list(self.sessions.keys())

    def encrypt_for_channel(self, message: str, channel: str, key: bytes, creator_fingerprint: str) -> bytes:
        """Encrypt message for a password-protected channel"""
        cipher = ChaCha20Poly1305(key)
        nonce = os.urandom(12)
        plaintext = message.encode('utf-8')
        return nonce + cipher.encrypt(nonce, plaintext, None)

    def decrypt_from_channel(self, data: bytes, channel: str, key: bytes, creator_fingerprint: str) -> str:
        """Decrypt message from a password-protected channel"""
        if len(data) < 12:
            raise ValueError("Invalid encrypted channel data — too short")

        nonce = data[:12]
        ciphertext = data[12:]

        cipher = ChaCha20Poly1305(key)
        plaintext = cipher.decrypt(nonce, ciphertext, None)
        return plaintext.decode('utf-8')

    def encrypt_with_key(self, data: bytes, key: bytes) -> bytes:
        """Encrypt data with a raw key (used for channel password change notifications)"""
        cipher = ChaCha20Poly1305(key)
        nonce = os.urandom(12)
        return nonce + cipher.encrypt(nonce, data, None)

    @staticmethod
    def derive_channel_key(password: str, channel: str) -> bytes:
        """
        Derive a deterministic channel key from password and channel name.
        Uses PBKDF2-HMAC-SHA256 with the channel name as salt.
        All peers with the same password derive the same key — no coordination needed.
        """
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt = channel.encode('utf-8')
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        return kdf.derive(password.encode('utf-8'))