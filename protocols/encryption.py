# encryption.py

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from os import urandom
import base64

# Shared key (16 bytes for AES-128)
SHARED_KEY = b'yuki_secret_key1'

# Encrypt payload (as string), return base64-encoded string
def encrypt_message(message: str, key: bytes = SHARED_KEY) -> str:
    iv = urandom(16)  # unique IV per message
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(message.encode()) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode()

# Decrypt base64-encoded payload, return original string
def decrypt_message(encoded: str, key: bytes = SHARED_KEY) -> str:
    raw = base64.b64decode(encoded.encode())
    iv = raw[:16]
    ciphertext = raw[16:]
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    decryptor = cipher.decryptor()
    return (decryptor.update(ciphertext) + decryptor.finalize()).decode()
