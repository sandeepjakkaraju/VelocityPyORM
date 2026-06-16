import base64
import os
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

class EncryptionService:
    def __init__(self, secret_key: str):
        # Java copy-pads/truncates to 32 bytes:
        # byte[] keyBytes = new byte[32];
        # byte[] userKeyBytes = secretKeyHex.getBytes(StandardCharsets.UTF_8);
        # System.arraycopy(userKeyBytes, 0, keyBytes, 0, Math.min(userKeyBytes.length, 32));
        key_bytes = secret_key.encode('utf-8')[:32]
        if len(key_bytes) < 32:
            key_bytes = key_bytes.ljust(32, b'\0')
        self.key = key_bytes

    def encrypt(self, value: str) -> str:
        if value is None:
            return None
        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(value.encode('utf-8')) + padder.finalize()
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        combined = iv + ciphertext
        return base64.b64encode(combined).decode('utf-8')

    def decrypt(self, base64_value: str) -> str:
        if base64_value is None:
            return None
        combined = base64.b64decode(base64_value)
        iv = combined[:16]
        ciphertext = combined[16:]
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded_data = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        data = unpadder.update(padded_data) + unpadder.finalize()
        return data.decode('utf-8')
