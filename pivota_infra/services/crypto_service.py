"""
Cryptographic Service for AP2 Protocol
Handles signature verification and receipt signing
"""
import base64
import json
import logging
from typing import Dict, Any, Optional
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger(__name__)

class CryptoService:
    """Cryptographic operations for AP2 protocol"""
    
    def __init__(self):
        self.platform_signing_key = os.getenv("PLATFORM_SIGNING_KEY")
        if not self.platform_signing_key:
            logger.warning("PLATFORM_SIGNING_KEY not configured - receipt signing disabled")
    
    @staticmethod
    def canonicalize_json(data: Dict[str, Any]) -> str:
        """
        Canonicalize JSON for consistent signing
        
        Args:
            data: Dictionary to canonicalize
            
        Returns:
            Canonical JSON string
        """
        return json.dumps(data, sort_keys=True, separators=(',', ':'))
    
    def verify_agent_signature(
        self,
        public_key: str,
        signature: str,
        payload: Dict[str, Any],
        algorithm: str = "ES256"
    ) -> bool:
        """
        Verify agent's signature on payload
        
        Args:
            public_key: Agent's public key (PEM format or base64 for Ed25519)
            signature: Base64-encoded signature
            payload: Data that was signed
            algorithm: ES256 or Ed25519
            
        Returns:
            True if signature is valid
        """
        try:
            # Canonicalize payload
            canonical_payload = self.canonicalize_json(payload)
            message_bytes = canonical_payload.encode('utf-8')
            
            # Decode signature from base64
            try:
                signature_bytes = base64.b64decode(signature)
            except Exception as e:
                logger.error(f"Invalid base64 signature: {e}")
                return False
            
            if algorithm == "ES256":
                # ES256 = ECDSA with P-256 curve and SHA-256
                try:
                    # Load public key from PEM
                    public_key_obj = serialization.load_pem_public_key(
                        public_key.encode('utf-8')
                    )
                    
                    # Verify it's an EC key
                    if not isinstance(public_key_obj, ec.EllipticCurvePublicKey):
                        logger.error("Public key is not an EC key for ES256")
                        return False
                    
                    # Verify signature
                    public_key_obj.verify(
                        signature_bytes,
                        message_bytes,
                        ec.ECDSA(hashes.SHA256())
                    )
                    
                    logger.info("✅ ES256 signature verified successfully")
                    return True
                    
                except InvalidSignature:
                    logger.warning("ES256 signature verification failed: Invalid signature")
                    return False
                except Exception as e:
                    logger.error(f"ES256 verification error: {e}")
                    return False
            
            elif algorithm == "Ed25519":
                # Ed25519 signature verification
                try:
                    # Load public key (expect raw 32-byte key or base64)
                    if len(public_key) == 32:
                        # Raw bytes
                        public_key_bytes = public_key.encode('latin1')
                    else:
                        # Try base64 decode
                        try:
                            public_key_bytes = base64.b64decode(public_key)
                        except:
                            # Try as PEM
                            public_key_obj = serialization.load_pem_public_key(
                                public_key.encode('utf-8')
                            )
                            if not isinstance(public_key_obj, ed25519.Ed25519PublicKey):
                                logger.error("Public key is not Ed25519")
                                return False
                            
                            # Verify with Ed25519
                            public_key_obj.verify(signature_bytes, message_bytes)
                            logger.info("✅ Ed25519 signature verified successfully")
                            return True
                    
                    # Load from raw bytes
                    public_key_obj = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
                    
                    # Verify signature
                    public_key_obj.verify(signature_bytes, message_bytes)
                    
                    logger.info("✅ Ed25519 signature verified successfully")
                    return True
                    
                except InvalidSignature:
                    logger.warning("Ed25519 signature verification failed: Invalid signature")
                    return False
                except Exception as e:
                    logger.error(f"Ed25519 verification error: {e}")
                    return False
            
            else:
                logger.error(f"Unsupported algorithm: {algorithm}")
                return False
                
        except Exception as e:
            logger.error(f"Signature verification failed: {e}")
            return False
    
    def sign_receipt(
        self,
        receipt_data: Dict[str, Any],
        algorithm: str = "ES256"
    ) -> Optional[str]:
        """
        Sign receipt with platform key
        
        Args:
            receipt_data: Receipt to sign
            algorithm: ES256 or Ed25519
            
        Returns:
            Base64-encoded signature or None if signing disabled
        """
        if not self.platform_signing_key:
            logger.warning("Receipt signing skipped - no platform key")
            return None
        
        try:
            # Canonicalize receipt
            canonical_receipt = self.canonicalize_json(receipt_data)
            message_bytes = canonical_receipt.encode('utf-8')
            
            if algorithm == "ES256":
                # Load private key from PEM
                try:
                    private_key = serialization.load_pem_private_key(
                        self.platform_signing_key.encode('utf-8'),
                        password=None
                    )
                    
                    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
                        logger.error("Platform key is not an EC private key")
                        return None
                    
                    # Sign with ES256
                    signature_bytes = private_key.sign(
                        message_bytes,
                        ec.ECDSA(hashes.SHA256())
                    )
                    
                    # Encode to base64
                    signature_b64 = base64.b64encode(signature_bytes).decode('utf-8')
                    
                    logger.info("✅ Receipt signed with ES256")
                    return signature_b64
                    
                except Exception as e:
                    logger.error(f"ES256 signing failed: {e}")
                    return None
            
            elif algorithm == "Ed25519":
                # Load Ed25519 private key
                try:
                    # Try as PEM first
                    try:
                        private_key = serialization.load_pem_private_key(
                            self.platform_signing_key.encode('utf-8'),
                            password=None
                        )
                    except:
                        # Try as raw bytes (base64 encoded)
                        key_bytes = base64.b64decode(self.platform_signing_key)
                        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(key_bytes)
                    
                    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
                        logger.error("Platform key is not an Ed25519 private key")
                        return None
                    
                    # Sign with Ed25519
                    signature_bytes = private_key.sign(message_bytes)
                    
                    # Encode to base64
                    signature_b64 = base64.b64encode(signature_bytes).decode('utf-8')
                    
                    logger.info("✅ Receipt signed with Ed25519")
                    return signature_b64
                    
                except Exception as e:
                    logger.error(f"Ed25519 signing failed: {e}")
                    return None
            
            else:
                logger.error(f"Unsupported signing algorithm: {algorithm}")
                return None
            
        except Exception as e:
            logger.error(f"Receipt signing failed: {e}")
            return None

# Singleton instance
crypto_service = CryptoService()

