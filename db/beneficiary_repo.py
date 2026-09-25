"""
Beneficiary Repository
Handles database operations for agent bank account information
Phase 6 - Payouts & Banking
"""

from typing import Optional, Dict, Any
from db.database import database

class BeneficiaryRepo:
    """Repository for agent_beneficiaries table operations"""
    
    async def get_default(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the default (most recently used/verified) bank account for an agent
        
        Args:
            agent_id: Agent identifier
            
        Returns:
            Bank account details or None
        """
        query = """
        SELECT 
            id,
            agent_id,
            method,
            currency,
            account_holder_name,
            iban,
            swift_bic,
            bank_name,
            bank_country,
            account_number,
            routing_number,
            account_number_last4,
            iban_preview,
            allow_share_with_merchants,
            verify_status,
            verified_at,
            metadata,
            created_at,
            updated_at
        FROM agent_beneficiaries
        WHERE agent_id = :aid
        ORDER BY 
            CASE WHEN verify_status = 'verified' THEN 0 ELSE 1 END,
            verified_at DESC NULLS LAST,
            created_at DESC
        LIMIT 1
        """
        
        result = await database.fetch_one(query=query, values={"aid": agent_id})
        return dict(result) if result else None
    
    async def get_by_method(self, agent_id: str, method: str = 'bank_wire', 
                           currency: str = 'USD') -> Optional[Dict[str, Any]]:
        """Get specific beneficiary by method and currency"""
        query = """
        SELECT * FROM agent_beneficiaries
        WHERE agent_id = :aid AND method = :method AND currency = :currency
        """
        
        result = await database.fetch_one(
            query=query,
            values={"aid": agent_id, "method": method, "currency": currency}
        )
        return dict(result) if result else None
    
    async def upsert_default(self, agent_id: str, data: dict) -> int:
        """
        Create or update bank account details for an agent
        
        Args:
            agent_id: Agent identifier
            data: Bank account details
            
        Returns:
            Beneficiary ID
        """
        # Process IBAN preview
        preview = None
        if data.get("iban"):
            s = data["iban"].replace(" ", "").upper()
            if len(s) >= 8:
                preview = f"{s[:4]}...{s[-4:]}"
            else:
                preview = s
        
        # Extract last 4 digits of account number
        last4 = None
        if data.get("account_number"):
            acc_num = str(data["account_number"]).strip()
            if len(acc_num) >= 4:
                last4 = acc_num[-4:]
        
        # Check if beneficiary exists
        method = data.get("method", "bank_wire")
        currency = data.get("currency", "USD")
        
        existing = await database.fetch_one(
            query="SELECT id FROM agent_beneficiaries WHERE agent_id = :aid AND method = :method AND currency = :currency",
            values={"aid": agent_id, "method": method, "currency": currency}
        )
        
        if existing:
            # Update existing beneficiary
            query = """
            UPDATE agent_beneficiaries SET
              account_holder_name = COALESCE(:name, account_holder_name),
              iban = :iban,
              swift_bic = :bic,
              bank_name = :bank,
              bank_country = :country,
              account_number = :acct,
              routing_number = :routing,
              account_number_last4 = COALESCE(:last4, account_number_last4),
              iban_preview = COALESCE(:preview, iban_preview),
              allow_share_with_merchants = :share,
              metadata = COALESCE(:meta, metadata, '{}'::jsonb),
              updated_at = NOW()
            WHERE agent_id = :aid AND method = :method AND currency = :currency
            RETURNING id
            """
            
            result = await database.fetch_one(
                query=query,
                values={
                    "aid": agent_id,
                    "method": method,
                    "currency": currency,
                    "name": data.get("account_holder_name"),
                    "iban": data.get("iban"),
                    "bic": data.get("swift_bic"),
                    "bank": data.get("bank_name"),
                    "country": data.get("bank_country"),
                    "acct": data.get("account_number"),
                    "routing": data.get("routing_number"),
                    "last4": last4,
                    "preview": preview,
                    "share": bool(data.get("allow_share_with_merchants", False)),
                    "meta": data.get("metadata")
                }
            )
            return result["id"] if result else existing["id"]
        else:
            # Insert new beneficiary
            query = """
            INSERT INTO agent_beneficiaries
            (agent_id, method, currency, account_holder_name, iban, swift_bic, 
             bank_name, bank_country, account_number, routing_number, 
             account_number_last4, iban_preview, allow_share_with_merchants, metadata)
            VALUES 
            (:aid, :method, :currency, :name, :iban, :bic, 
             :bank, :country, :acct, :routing, 
             :last4, :preview, :share, COALESCE(:meta, '{}'::jsonb))
            RETURNING id
            """
            
            result = await database.fetch_one(
                query=query,
                values={
                    "aid": agent_id,
                    "method": method,
                    "currency": currency,
                    "name": data.get("account_holder_name"),
                    "iban": data.get("iban"),
                    "bic": data.get("swift_bic"),
                    "bank": data.get("bank_name"),
                    "country": data.get("bank_country"),
                    "acct": data.get("account_number"),
                    "routing": data.get("routing_number"),
                    "last4": last4,
                    "preview": preview,
                    "share": bool(data.get("allow_share_with_merchants", False)),
                    "meta": data.get("metadata")
                }
            )
            return result["id"] if result else 0
    
    async def set_share(self, agent_id: str, allow: bool) -> bool:
        """
        Update sharing permission for all agent's bank accounts
        
        Args:
            agent_id: Agent identifier
            allow: Whether to allow merchants to see bank details
            
        Returns:
            True if successful
        """
        query = """
        UPDATE agent_beneficiaries 
        SET allow_share_with_merchants = :allow, updated_at = NOW()
        WHERE agent_id = :aid
        """
        
        await database.execute(
            query=query,
            values={"allow": allow, "aid": agent_id}
        )
        return True
    
    async def verify(self, agent_id: str, beneficiary_id: int, 
                     status: str = 'verified') -> bool:
        """
        Update verification status of a beneficiary
        
        Args:
            agent_id: Agent identifier (for security check)
            beneficiary_id: Beneficiary record ID
            status: New verification status
            
        Returns:
            True if successful
        """
        query = """
        UPDATE agent_beneficiaries 
        SET 
            verify_status = CAST(:status AS text),
            verified_at = CASE WHEN CAST(:status AS text) = 'verified' THEN NOW() ELSE verified_at END,
            updated_at = NOW()
        WHERE id = :id AND agent_id = :aid
        """
        
        await database.execute(
            query=query,
            values={
                "status": status,
                "id": beneficiary_id,
                "aid": agent_id
            }
        )
        return True
    
    async def delete(self, agent_id: str, beneficiary_id: int) -> bool:
        """
        Delete a beneficiary record
        
        Args:
            agent_id: Agent identifier (for security check)
            beneficiary_id: Beneficiary record ID
            
        Returns:
            True if successful
        """
        query = """
        DELETE FROM agent_beneficiaries 
        WHERE id = :id AND agent_id = :aid
        """
        
        result = await database.execute(
            query=query,
            values={"id": beneficiary_id, "aid": agent_id}
        )
        return True
    
    async def list_by_agent(self, agent_id: str) -> list:
        """Get all beneficiaries for an agent"""
        query = """
        SELECT * FROM agent_beneficiaries
        WHERE agent_id = :aid
        ORDER BY created_at DESC
        """
        
        results = await database.fetch_all(query=query, values={"aid": agent_id})
        return [dict(r) for r in results]
