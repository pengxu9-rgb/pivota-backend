"""
Encryption utilities for sensitive data
Uses Fernet symmetric encryption
"""
import os
import json
import base64
import logging
from typing import Dict, Any, Optional
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)


class EncryptionManager:
    """Manages encryption/decryption of sensitive data"""
    
    def __init__(self):
        # Get encryption key from environment or generate
        self.master_key = os.environ.get('ENCRYPTION_MASTER_KEY')
        if not self.master_key:
            # In production, this should come from a secure key management service
            logger.warning("⚠️ No ENCRYPTION_MASTER_KEY found, generating temporary key")
            self.master_key = Fernet.generate_key().decode()
            
        self.fernet = self._create_fernet(self.master_key)
    
    def _create_fernet(self, key_string: str) -> Fernet:
        """Create Fernet instance from key string"""
        if len(key_string) == 44 and key_string.endswith('='):
            # Already a valid Fernet key
            return Fernet(key_string.encode())
        else:
            # Derive key from password
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b'pivota_salt_v1',  # In production, use random salt per merchant
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(key_string.encode()))
            return Fernet(key)
    
    def encrypt(self, data: Dict[str, Any]) -> str:
        """Encrypt dictionary data to string"""
        try:
            json_str = json.dumps(data, sort_keys=True)
            encrypted_bytes = self.fernet.encrypt(json_str.encode())
            return encrypted_bytes.decode()
        except Exception as e:
            logger.error(f"Encryption error: {str(e)}")
            raise
    
    def decrypt(self, encrypted_data: str) -> Dict[str, Any]:
        """Decrypt string back to dictionary"""
        try:
            decrypted_bytes = self.fernet.decrypt(encrypted_data.encode())
            json_str = decrypted_bytes.decode()
            return json.loads(json_str)
        except Exception as e:
            logger.error(f"Decryption error: {str(e)}")
            raise
    
    def mask_sensitive_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Mask sensitive fields for display"""
        masked = {}
        sensitive_fields = [
            'secret_key', 'api_key', 'access_token', 'private_key', 
            'consumer_secret', 'client_secret', 'webhook_secret',
            'password', 'token'
        ]
        
        for key, value in data.items():
            if isinstance(value, dict):
                masked[key] = self.mask_sensitive_data(value)
            elif isinstance(value, str) and any(field in key.lower() for field in sensitive_fields):
                # Show first 4 and last 4 characters
                if len(value) > 12:
                    masked[key] = f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"
                else:
                    masked[key] = '*' * len(value)
            else:
                masked[key] = value
        
        return masked


# Global instance
_encryption_manager = None


def get_encryption_manager() -> EncryptionManager:
    """Get global encryption manager instance"""
    global _encryption_manager
    if _encryption_manager is None:
        _encryption_manager = EncryptionManager()
    return _encryption_manager


def encrypt_data(data: Dict[str, Any]) -> str:
    """Encrypt data using global manager"""
    return get_encryption_manager().encrypt(data)


def decrypt_data(encrypted_data: str) -> Dict[str, Any]:
    """Decrypt data using global manager"""
    return get_encryption_manager().decrypt(encrypted_data)


def mask_credentials(data: Dict[str, Any]) -> Dict[str, Any]:
    """Mask sensitive credentials for display"""
    return get_encryption_manager().mask_sensitive_data(data)
