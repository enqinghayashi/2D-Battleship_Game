import struct
import os # For nonce generation
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# Packet format:
# | seq (4 bytes) | type (1 byte) | length (2 bytes) | nonce (16 bytes) | encrypted_payload (variable) | checksum (4 bytes) |

HEADER_FORMAT = "!IBH"  # seq: uint32, type: uint8, length: uint16
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
CHECKSUM_SIZE = 4
NONCE_SIZE = 16 # AES block size, common for CTR IV/nonce

# --- Encryption Configuration ---
# For AES-256, the key must be 32 bytes.
SHARED_KEY = b'my_super_secret_aes_256_key_32bb' 
if len(SHARED_KEY) not in (16, 24, 32):
    raise ValueError("SHARED_KEY must be 16, 24, or 32 bytes long for AES.")

# Packet types
PKT_TYPE_GAME = 1
PKT_TYPE_CHAT = 2

def _encrypt(key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()

def _decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()

def calc_checksum(data: bytes) -> int:
    """Simple sum-based checksum (modulo 2^32)."""
    return sum(data) % (2**32)

def build_packet(seq: int, pkt_type: int, payload: bytes) -> bytes:
    # Encrypt the payload
    nonce = os.urandom(NONCE_SIZE)
    encrypted_payload = _encrypt(SHARED_KEY, nonce, payload)
    
    # The payload for the packet structure is now nonce + encrypted_payload
    full_payload = nonce + encrypted_payload
    
    header = struct.pack(HEADER_FORMAT, seq, pkt_type, len(full_payload))
    body = header + full_payload # Checksum is over header + nonce + encrypted_payload
    checksum = calc_checksum(body)
    return body + struct.pack("!I", checksum)

def parse_packet(packet: bytes):
    """Parse and verify a packet. Returns (seq, pkt_type, decrypted_payload) or raises ValueError."""
    if len(packet) < HEADER_SIZE + NONCE_SIZE + CHECKSUM_SIZE: # Minimum payload is empty, but nonce is always there
        raise ValueError(f"Packet too short. Min length {HEADER_SIZE + NONCE_SIZE + CHECKSUM_SIZE}, got {len(packet)}")
    
    header = packet[:HEADER_SIZE]
    seq, pkt_type, length = struct.unpack(HEADER_FORMAT, header)

    # The length field refers to (nonce + encrypted_payload)
    if len(packet) < HEADER_SIZE + length + CHECKSUM_SIZE:
        raise ValueError(f"Packet shorter than specified by header. Expected {HEADER_SIZE + length + CHECKSUM_SIZE}, got {len(packet)}")

    full_payload_with_nonce = packet[HEADER_SIZE : HEADER_SIZE + length]
    checksum_bytes = packet[HEADER_SIZE + length : HEADER_SIZE + length + CHECKSUM_SIZE]

    if len(full_payload_with_nonce) != length: # Clarify the length once again
        raise ValueError("Malformed packet: payload length mismatch")
    if len(checksum_bytes) != CHECKSUM_SIZE:
        raise ValueError("Malformed packet: checksum length mismatch")

    body = header + full_payload_with_nonce # Checksum is calculated over header + nonce + encrypted_payload
    expected_checksum = struct.unpack("!I", checksum_bytes)[0]
    if calc_checksum(body) != expected_checksum:
        raise ValueError("Checksum mismatch")

    # Extract nonce and the actual encrypted payload
    if len(full_payload_with_nonce) < NONCE_SIZE:
        raise ValueError("Malformed packet: payload too short to contain nonce")
        
    nonce = full_payload_with_nonce[:NONCE_SIZE]
    encrypted_original_payload = full_payload_with_nonce[NONCE_SIZE:]
    
    # Decrypt the original payload
    try:
        decrypted_payload = _decrypt(SHARED_KEY, nonce, encrypted_original_payload)
    except Exception as e: # Catch potential decryption errors
        raise ValueError(f"Decryption failed: {e}")
        
    return seq, pkt_type, decrypted_payload

# Example usage:
# pkt = build_packet(1, PKT_TYPE_CHAT, b"hayalin:hello world")
# seq, pkt_type, payload = parse_packet(pkt)

