from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from os import urandom
import base64

# the secret key, need to change!!!!!!!!!!
SHARED_KEY = b'yuki_secret_key1'  # 16 bytes long

# encrypt the messages
def encrypt_message(message: str, key: bytes = SHARED_KEY) -> str:
    iv = urandom(16) # To make sure different result of encrypt messages
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))# AES, CTR
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(message.encode()) + encryptor.finalize()
    encoded = base64.b64encode(iv + ciphertext).decode() # Put IV and ciphertext together and use base64 to transfer

    return encoded

# To decrypt all messages, receive the combination of iv and ciphertext
def decrypt_message(encoded: str, key: bytes = SHARED_KEY) -> str:
    raw = base64.b64decode(encoded.encode()) # change base64 to iv + ciphertext
    iv = raw[:16]
    ciphertext = raw[16:]
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv)) # Use key
    decryptor = cipher.decryptor()
    message = (decryptor.update(ciphertext) + decryptor.finalize()).decode()

 
    return message


