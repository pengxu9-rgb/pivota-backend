"""
Wallet Service for X-402 Protocol
Handles wallet address validation and management
"""
import re
import logging
from typing import Tuple, Optional, Dict, Any

from db.database import database

logger = logging.getLogger(__name__)


class WalletService:
    """Wallet address validation and management"""
    
    @staticmethod
    def validate_ethereum(address: str) -> Tuple[bool, Optional[str]]:
        """Validate Ethereum address (0x + 40 hex chars)"""
        if not address:
            return False, "Address is required"
        
        if not address.startswith('0x'):
            return False, "Ethereum address must start with 0x"
        
        if len(address) != 42:
            return False, "Ethereum address must be 42 characters"
        
        if not re.match(r'^0x[a-fA-F0-9]{40}$', address):
            return False, "Invalid Ethereum address format"
        
        return True, None
    
    @staticmethod
    def validate_solana(address: str) -> Tuple[bool, Optional[str]]:
        """Validate Solana address (base58, 32-44 chars)"""
        if not address:
            return False, "Address is required"
        
        if len(address) < 32 or len(address) > 44:
            return False, "Solana address must be 32-44 characters"
        
        # Basic base58 check (no 0, O, I, l)
        if re.search(r'[0OIl]', address):
            return False, "Invalid Solana address (contains 0, O, I, or l)"
        
        return True, None
    
    @staticmethod
    def validate_bitcoin(address: str) -> Tuple[bool, Optional[str]]:
        """Validate Bitcoin address (basic check)"""
        if not address:
            return False, "Address is required"
        
        # Legacy (1...), SegWit (3...), Bech32 (bc1...)
        if not (address.startswith('1') or address.startswith('3') or address.startswith('bc1')):
            return False, "Invalid Bitcoin address prefix"
        
        if len(address) < 26 or len(address) > 62:
            return False, "Invalid Bitcoin address length"
        
        return True, None
    
    def validate_address(
        self,
        network: str,
        address: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate wallet address for given network
        
        Args:
            network: ethereum, solana, bitcoin
            address: wallet address string
            
        Returns:
            (is_valid, error_message)
        """
        network = network.lower()
        
        if network == 'ethereum':
            return self.validate_ethereum(address)
        elif network == 'solana':
            return self.validate_solana(address)
        elif network == 'bitcoin':
            return self.validate_bitcoin(address)
        else:
            return False, f"Unsupported network: {network}"
    
    async def get_merchant_wallet(
        self,
        merchant_id: str,
        network: str
    ) -> Optional[Dict[str, Any]]:
        """Get active wallet for merchant on specified network"""
        try:
            wallet = await database.fetch_one(
                """SELECT * FROM merchant_wallets 
                   WHERE merchant_id = :merchant_id 
                   AND network = :network 
                   AND status = 'active'
                   ORDER BY verified_at DESC NULLS LAST
                   LIMIT 1""",
                {"merchant_id": merchant_id, "network": network}
            )
            return dict(wallet) if wallet else None
        except Exception as e:
            logger.error(f"Error fetching merchant wallet: {e}")
            return None
    
    async def get_agent_wallet(
        self,
        agent_id: str,
        network: str
    ) -> Optional[Dict[str, Any]]:
        """Get active wallet for agent on specified network"""
        try:
            wallet = await database.fetch_one(
                """SELECT * FROM agent_wallets 
                   WHERE agent_id = :agent_id 
                   AND network = :network 
                   AND status = 'active'
                   ORDER BY verified_at DESC NULLS LAST
                   LIMIT 1""",
                {"agent_id": agent_id, "network": network}
            )
            return dict(wallet) if wallet else None
        except Exception as e:
            logger.error(f"Error fetching agent wallet: {e}")
            return None

    async def verify_agent_wallet(
        self,
        agent_id: str,
        wallet_address: str
    ) -> bool:
        """
        Authorize an agent-supplied wallet address for a payment action.

        Returns True only when the address is registered to THIS agent and the
        wallet is active. A caller-supplied address that belongs to another
        agent, is unknown, or is not active must not be authorized. Fails
        closed (returns False) on missing input or any query error.
        """
        if not agent_id or not wallet_address:
            return False

        try:
            wallet = await database.fetch_one(
                """SELECT wallet_id FROM agent_wallets
                   WHERE agent_id = :agent_id
                   AND address = :address
                   AND status = 'active'
                   LIMIT 1""",
                {"agent_id": agent_id, "address": wallet_address}
            )
            return wallet is not None
        except Exception as e:
            logger.error(f"Error verifying agent wallet: {e}")
            return False


# Singleton instance
wallet_service = WalletService()










