"""
Cryptographic Service for AP2 Protocol and Connector Credentials
Handles signature verification, receipt signing, and credential encryption
"""
import base64
import json
import logging
from typing import Dict, Any, Optional
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidSignature

logger = logging.getLogger(__name__)

class CryptoService:
    """Cryptographic operations for AP2 protocol and connector credentials"""
    
    def __init__(self):
        self.platform_signing_key = os.getenv("PLATFORM_SIGNING_KEY")
        if not self.platform_signing_key:
            logger.warning("PLATFORM_SIGNING_KEY not configured - receipt signing disabled")
        
        # Initialize AES-GCM for connector credentials encryption
        connector_key_b64 = os.getenv("CONNECTOR_CREDENTIALS_KEY")
        if connector_key_b64:
            try:
                self.connector_key = base64.b64decode(connector_key_b64)
                if len(self.connector_key) != 32:
                    logger.error(f"CONNECTOR_CREDENTIALS_KEY must be 32 bytes (got {len(self.connector_key)})")
                    self.connector_key = None
            except Exception as e:
                logger.error(f"Failed to decode CONNECTOR_CREDENTIALS_KEY: {e}")
                self.connector_key = None
        else:
            logger.warning("CONNECTOR_CREDENTIALS_KEY not set - connector credential encryption disabled")
            self.connector_key = None
    
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

    @staticmethod
    def _verify_es256(public_key_obj, signature_bytes: bytes, message_bytes: bytes) -> bool:
        """
        Verify an ES256 (ECDSA P-256 / SHA-256) signature accepting BOTH encodings:

        - **ASN.1 DER** — OpenSSL's default and what this service's own
          ``sign_receipt`` produces; and
        - **raw r||s (IEEE P1363)** — a fixed 64-byte concatenation, the form
          emitted by JOSE (RFC 7515), WebCrypto, and essentially every
          standards-based DID/VC agent.

        The bytes are tried as DER first, then — only for a 64-byte signature —
        as raw r||s converted to DER. Returns True on the first that verifies.
        No false negatives: a genuine DER signature verifies on the first branch,
        a genuine raw one on the second; the 64-byte raw form is not valid DER,
        so the ordering can't mask it. Invalid signatures fail both and return
        False (never raises).
        """
        candidates = [signature_bytes]
        if len(signature_bytes) == 64:
            r = int.from_bytes(signature_bytes[:32], "big")
            s = int.from_bytes(signature_bytes[32:], "big")
            candidates.append(encode_dss_signature(r, s))

        for candidate in candidates:
            try:
                public_key_obj.verify(candidate, message_bytes, ec.ECDSA(hashes.SHA256()))
                return True
            except (InvalidSignature, ValueError):
                # ValueError: `candidate` wasn't parseable as DER (e.g. raw bytes
                # fed to the DER branch) — try the next candidate.
                continue
        return False

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
                    
                    # Accept BOTH ASN.1 DER and raw r||s (JOSE / WebCrypto / DID,
                    # 64-byte) ES256 signatures. Standards-based external agents —
                    # exactly the callers the AP2 signed rail targets — emit the
                    # raw form, which the DER-only verify silently rejected.
                    if self._verify_es256(public_key_obj, signature_bytes, message_bytes):
                        logger.info("✅ ES256 signature verified successfully")
                        return True

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

    def validate_public_key(self, public_key: str, algorithm: str = "ES256") -> None:
        """Raise ``ValueError`` unless ``public_key`` is loadable for ``algorithm``
        the SAME way ``verify_agent_signature`` loads it — so a key that validates
        here is provably usable for verification (no "registered but broken" key).

        - ``ES256``: a PEM SubjectPublicKeyInfo EC key on the **P-256** curve.
        - ``Ed25519``: a raw 32-byte key, base64 of 32 bytes, or a PEM Ed25519 key
          (mirrors the formats ``verify_agent_signature`` accepts).
        """
        if not isinstance(public_key, str) or not public_key.strip():
            raise ValueError("public_key must be a non-empty string")

        if algorithm == "ES256":
            try:
                obj = serialization.load_pem_public_key(public_key.encode("utf-8"))
            except Exception as e:
                raise ValueError(f"ES256 public_key must be a PEM SubjectPublicKeyInfo key: {e}")
            if not isinstance(obj, ec.EllipticCurvePublicKey):
                raise ValueError("ES256 public_key is not an EC key")
            if not isinstance(obj.curve, ec.SECP256R1):
                raise ValueError("ES256 public_key must use the P-256 (secp256r1) curve")
            return

        if algorithm == "Ed25519":
            # Raw 32 bytes carried as latin1 (matches verify_agent_signature).
            if len(public_key) == 32:
                ed25519.Ed25519PublicKey.from_public_bytes(public_key.encode("latin1"))
                return
            # base64 of exactly 32 bytes.
            try:
                raw = base64.b64decode(public_key, validate=True)
            except Exception:
                raw = None
            if raw is not None and len(raw) == 32:
                ed25519.Ed25519PublicKey.from_public_bytes(raw)
                return
            # PEM Ed25519.
            try:
                obj = serialization.load_pem_public_key(public_key.encode("utf-8"))
            except Exception as e:
                raise ValueError(f"Ed25519 public_key must be raw-32 / base64-32 / PEM: {e}")
            if not isinstance(obj, ed25519.Ed25519PublicKey):
                raise ValueError("Ed25519 public_key is not an Ed25519 key")
            return

        raise ValueError(f"Unsupported algorithm: {algorithm} (use ES256 or Ed25519)")

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
    
    def encrypt_json_secret(self, data: Dict[str, Any]) -> str:
        """
        Encrypt JSON data using AES-GCM for connector credentials
        
        Args:
            data: Dictionary to encrypt
            
        Returns:
            Base64-encoded encrypted data with nonce prepended
            
        Raises:
            RuntimeError: If encryption key is not configured
        """
        if not self.connector_key:
            raise RuntimeError("CONNECTOR_CREDENTIALS_KEY not configured - cannot encrypt")
        
        try:
            # Serialize to JSON
            json_str = json.dumps(data, sort_keys=True)
            plaintext = json_str.encode('utf-8')
            
            # Generate random nonce (96 bits for GCM)
            aesgcm = AESGCM(self.connector_key)
            nonce = os.urandom(12)
            
            # Encrypt
            ciphertext = aesgcm.encrypt(nonce, plaintext, None)
            
            # Prepend nonce to ciphertext and encode as base64
            encrypted_blob = nonce + ciphertext
            encoded = base64.b64encode(encrypted_blob).decode('utf-8')
            
            logger.info("✅ Credentials encrypted successfully")
            return encoded
            
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise RuntimeError(f"Failed to encrypt credentials: {e}")
    
    def decrypt_json_secret(self, encoded_data: str) -> Dict[str, Any]:
        """
        Decrypt AES-GCM encrypted JSON data
        
        Args:
            encoded_data: Base64-encoded encrypted data (nonce + ciphertext)
            
        Returns:
            Decrypted dictionary
            
        Raises:
            RuntimeError: If decryption fails
        """
        if not self.connector_key:
            raise RuntimeError("CONNECTOR_CREDENTIALS_KEY not configured - cannot decrypt")
        
        try:
            # Decode from base64
            encrypted_blob = base64.b64decode(encoded_data)
            
            # Extract nonce and ciphertext
            nonce = encrypted_blob[:12]
            ciphertext = encrypted_blob[12:]
            
            # Decrypt
            aesgcm = AESGCM(self.connector_key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            
            # Parse JSON
            json_str = plaintext.decode('utf-8')
            data = json.loads(json_str)
            
            logger.info("✅ Credentials decrypted successfully")
            return data
            
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise RuntimeError(f"Failed to decrypt credentials: {e}")

# Singleton instance
crypto_service = CryptoService()

