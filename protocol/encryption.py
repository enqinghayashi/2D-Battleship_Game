import base64
from cryptography.fernet import Fernet

SECRET_KEY = b"fZG9F5iRrwkwd5S6aenhimqN8Og0JZWmHkUp-gkuSLU="  
fernet = Fernet(SECRET_KEY)

def encrypt_message(message: str) -> str:
    if not isinstance(message, str):
        raise ValueError("encrypt_message expects a string")
    encrypted = fernet.encrypt(message.encode('utf-8'))
    print(f"[ENCRYPT] '{message}' --> {encrypted.decode('utf-8')}")
    return encrypted.decode('utf-8')

def decrypt_message(token: str) -> str:
    if not isinstance(token, str):
        raise ValueError("decrypt_message expects a string")
    decrypted = fernet.decrypt(token.encode('utf-8'))
    print(f"[DECRYPT] {token} --> '{decrypted.decode('utf-8')}'")
    return decrypted.decode('utf-8')