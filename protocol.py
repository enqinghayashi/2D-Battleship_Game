import struct

# Packet format:
# | seq (4 bytes) | type (1 byte) | length (2 bytes) | payload (variable) | checksum (4 bytes) |

HEADER_FORMAT = "!IBH"  # seq: uint32, type: uint8, length: uint16
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
CHECKSUM_SIZE = 4

# Packet types
PKT_TYPE_GAME = 1
PKT_TYPE_CHAT = 2

def calc_checksum(data: bytes) -> int:
    """Simple sum-based checksum (modulo 2^32)."""
    return sum(data) % (2**32)

def build_packet(seq: int, pkt_type: int, payload: bytes) -> bytes:
    header = struct.pack(HEADER_FORMAT, seq, pkt_type, len(payload))
    body = header + payload
    checksum = calc_checksum(body)
    return body + struct.pack("!I", checksum)

def parse_packet(packet: bytes):
    """Parse and verify a packet. Returns (seq, pkt_type, payload) or raises ValueError."""
    if len(packet) < HEADER_SIZE + CHECKSUM_SIZE:
        raise ValueError("Packet too short")
    header = packet[:HEADER_SIZE]
    seq, pkt_type, length = struct.unpack(HEADER_FORMAT, header)
    payload = packet[HEADER_SIZE:HEADER_SIZE+length]
    checksum_bytes = packet[HEADER_SIZE+length:HEADER_SIZE+length+CHECKSUM_SIZE]
    if len(payload) != length or len(checksum_bytes) != CHECKSUM_SIZE:
        raise ValueError("Malformed packet")
    body = header + payload
    checksum = struct.unpack("!I", checksum_bytes)[0]
    if calc_checksum(body) != checksum:
        raise ValueError("Checksum mismatch")
    return seq, pkt_type, payload

# Example usage:
# pkt = build_packet(1, PKT_TYPE_CHAT, b"alice:hello world")
# seq, pkt_type, payload = parse_packet(pkt)
