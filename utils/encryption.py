"""
Encryption utilities for sensitive data
Handles encryption/decryption of bank accounts, tax IDs, etc.
"""

import os
import base64
from cryptography.fernet import Fernet
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Get encryption key from environment or generate one
# In production, this MUST be set in environment variables
ENCRYPTION_KEY = os.getenv("DATA_ENCRYPTION_KEY")

if not ENCRYPTION_KEY:
    # WARNING: Generate a key for development
    # In production, use a proper KMS or environment variable
    logger.warning("⚠️ DATA_ENCRYPTION_KEY not set - using generated key (NOT SECURE for production)")
    ENCRYPTION_KEY = Fernet.generate_key().decode()

# Ensure key is bytes
if isinstance(ENCRYPTION_KEY, str):
    ENCRYPTION_KEY = ENCRYPTION_KEY.encode()

# Create Fernet instance
try:
    cipher = Fernet(ENCRYPTION_KEY)
except Exception as e:
    logger.error(f"Failed to initialize encryption: {e}")
    # Fallback: generate new key
    ENCRYPTION_KEY = Fernet.generate_key()
    cipher = Fernet(ENCRYPTION_KEY)
    logger.warning("Using fallback encryption key - data will not be decryptable after restart!")


def encrypt_sensitive_data(data: str) -> str:
    """
    Encrypt sensitive data before storing in database
    
    Args:
        data: Plain text string to encrypt
        
    Returns:
        Base64-encoded encrypted string
        
    Example:
        encrypted = encrypt_sensitive_data("123-45-6789")
        # Returns: "gAAAAABh..."
    """
    if not data:
        return ""
    
    try:
        # Encrypt the data
        encrypted_bytes = cipher.encrypt(data.encode('utf-8'))
        
        # Return as base64 string for easy database storage
        return encrypted_bytes.decode('utf-8')
    
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        # Fallback: return base64 encoded (NOT SECURE - better than plaintext)
        return base64.b64encode(data.encode('utf-8')).decode('utf-8')


def decrypt_sensitive_data(encrypted_data: str) -> Optional[str]:
    """
    Decrypt sensitive data retrieved from database
    
    Args:
        encrypted_data: Encrypted string from database
        
    Returns:
        Decrypted plain text string or None if decryption fails
        
    Example:
        decrypted = decrypt_sensitive_data("gAAAAABh...")
        # Returns: "123-45-6789"
    """
    if not encrypted_data:
        return None
    
    try:
        # Decrypt the data
        decrypted_bytes = cipher.decrypt(encrypted_data.encode('utf-8'))
        
        # Return as string
        return decrypted_bytes.decode('utf-8')
    
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        # Try base64 decode as fallback
        try:
            return base64.b64decode(encrypted_data.encode('utf-8')).decode('utf-8')
        except:
            logger.error("Base64 decryption also failed")
            return None


def mask_sensitive_data(data: str, visible_chars: int = 4) -> str:
    """
    Mask sensitive data for display (show only last N characters)
    
    Args:
        data: Sensitive data to mask
        visible_chars: Number of characters to show at end
        
    Returns:
        Masked string like "••••1234"
        
    Example:
        masked = mask_sensitive_data("123-45-6789", 4)
        # Returns: "••••6789"
    """
    if not data:
        return ""
    
    if len(data) <= visible_chars:
        return "••••"

    return "••••" + data[-visible_chars:]


# Characters any masking scheme in this codebase uses as filler. A real
# credential from Stripe/Adyen/Checkout/PayPal is base62-ish and contains
# neither, which is what makes `is_masked_credential` below safe to key on.
_MASK_FILLERS = ("*", "•")


def mask_credential(value: Optional[str], visible_chars: int = 4) -> Optional[str]:
    """Mask a stored credential for display, preserving the last few characters.

    Uses `*` rather than the `•` of `mask_sensitive_data` deliberately: the PSP
    payloads that call this already render `secret_key` with asterisks, and an
    operator comparing two credential fields in one response should not have to
    wonder whether the differing glyphs mean differing treatment. Both forms are
    recognised by `is_masked_credential`, so the choice is cosmetic and cannot
    become a correctness gap.

    Returns None for a missing value so callers can distinguish "no credential
    stored" from "a credential we are not showing you".

    FIXED-WIDTH, and reveals nothing below a floor. Review found two leaks in
    the obvious implementation:

    - `"*" * (len(value) - 4) + value[-4:]` publishes the credential's EXACT
      LENGTH, which is a fingerprint of the issuing provider and a search-space
      reduction for anyone attacking it. The mask is now a constant 12
      asterisks regardless of input length.
    - The same expression revealed 4 of 5 characters of a 5-character secret.
      `/admin/psp/connect` enforces `len >= 8`, but other writers do not
      (`PUT /merchant/integrations/psp/{psp_id}` only requires non-empty), so
      short values do reach here. Anything under 8 characters now reveals
      NOTHING.

    Non-`str` input returns None rather than raising. This is defensive on
    purpose: the sole caller sits in a loop inside `/psps/all`, whose outer
    handler turns any exception into an empty list, so one malformed row would
    have silently emptied the whole PSP view — a fabrication-shaped failure
    rather than a visible one.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) < 8:
        return "*" * 12
    return "*" * 12 + value[-visible_chars:]


def is_masked_credential(value: Optional[str]) -> bool:
    """True if `value` is a mask this codebase produced, not a real credential.

    WHY THIS EXISTS, AND WHY MASKING IS UNSAFE WITHOUT IT. The employee portal's
    PSP form pre-fills its api_key input from whatever `/psps/all` returned and
    posts that value straight back on save. So masking a credential in a
    response, on its own, converts a disclosure bug into a DESTRUCTION bug: the
    next save writes `****abcd` over the real key. Any write path accepting a
    credential that a masked read could have populated must call this and keep
    the stored value instead.

    Keyed on filler characters rather than on an exact length or prefix match,
    so it survives a change of masking style and cannot be defeated by a
    credential whose length happens to line up.
    """
    if not value:
        return False
    return any(filler in value for filler in _MASK_FILLERS)


def validate_tax_id(tax_id: str, tax_id_type: str, country: str) -> bool:
    """
    Validate tax ID format
    
    Args:
        tax_id: Tax ID number
        tax_id_type: Type (ssn, ein, vat, etc.)
        country: Country code
        
    Returns:
        True if format is valid
    """
    import re
    
    if tax_id_type == 'ssn' and country == 'USA':
        # SSN: XXX-XX-XXXX or XXXXXXXXX
        pattern = r'^\d{3}-?\d{2}-?\d{4}$'
        return bool(re.match(pattern, tax_id))
    
    elif tax_id_type == 'ein' and country == 'USA':
        # EIN: XX-XXXXXXX or XXXXXXXXX
        pattern = r'^\d{2}-?\d{7}$'
        return bool(re.match(pattern, tax_id))
    
    elif tax_id_type == 'vat':
        # EU VAT: Country code + 8-12 digits
        # Example: GB123456789, DE123456789
        pattern = r'^[A-Z]{2}\d{8,12}$'
        return bool(re.match(pattern, tax_id))
    
    # For other types, just check it's not empty
    return len(tax_id) > 0


def validate_bank_routing_number(routing: str, country: str = 'USA') -> bool:
    """
    Validate bank routing number format
    
    Args:
        routing: Routing number
        country: Country code
        
    Returns:
        True if format is valid
    """
    import re
    
    if country == 'USA':
        # US routing number: 9 digits
        pattern = r'^\d{9}$'
        return bool(re.match(pattern, routing))
    
    # For other countries, just check it's not empty
    return len(routing) > 0


def validate_iban(iban: str) -> bool:
    """
    Validate IBAN format
    
    Args:
        iban: International Bank Account Number
        
    Returns:
        True if format is valid
    """
    import re
    
    # Remove spaces and convert to uppercase
    iban = iban.replace(' ', '').upper()
    
    # IBAN: 2 letter country code + 2 check digits + up to 30 alphanumeric
    pattern = r'^[A-Z]{2}\d{2}[A-Z0-9]{1,30}$'
    
    if not re.match(pattern, iban):
        return False
    
    # Could add checksum validation here for full IBAN validation
    # For now, just format check
    return True


def validate_swift_code(swift: str) -> bool:
    """
    Validate SWIFT/BIC code format
    
    Args:
        swift: SWIFT/BIC code
        
    Returns:
        True if format is valid
    """
    import re
    
    # SWIFT: 8 or 11 characters (AAAA BB CC DDD)
    # AAAA: Bank code (4 letters)
    # BB: Country code (2 letters)
    # CC: Location code (2 letters or digits)
    # DDD: Branch code (3 letters or digits, optional)
    pattern = r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$'
    
    return bool(re.match(pattern, swift.upper()))


def generate_encryption_key() -> str:
    """
    Generate a new encryption key for DATA_ENCRYPTION_KEY
    
    Run this once and save the output to your environment variables
    
    Returns:
        Base64-encoded encryption key
    """
    key = Fernet.generate_key()
    return key.decode('utf-8')


if __name__ == "__main__":
    # Generate a new encryption key
    print("Generated Encryption Key:")
    print(generate_encryption_key())
    print("\nAdd this to your Railway environment variables as:")
    print("DATA_ENCRYPTION_KEY=<key_above>")

