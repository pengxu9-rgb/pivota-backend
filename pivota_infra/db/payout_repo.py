"""
Payout Repository
Handles database operations for agent payouts
Phase 6 - Payouts & Banking
"""

from typing import List, Optional, Dict, Any
from datetime import date
from db.database import database
from sqlalchemy import text

class PayoutRepo:
    """Repository for agent_payouts table operations"""
    
    async def list(self, merchant_id: Optional[str] = None, agent_id: Optional[str] = None, 
                   status: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """
        List payouts with optional filters
        
        Args:
            merchant_id: Filter by merchant
            agent_id: Filter by agent
            status: Filter by status (pending, uploaded, paid)
            limit: Maximum number of results
            offset: Number of results to skip
            
        Returns:
            List of payout records
        """
        conditions = []
        params = {"limit": limit, "offset": offset}
        
        if merchant_id:
            conditions.append("merchant_id = :mid")
            params["mid"] = merchant_id
        if agent_id:
            conditions.append("agent_id = :aid")
            params["aid"] = agent_id
        if status:
            conditions.append("status = :st")
            params["st"] = status
            
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        query = f"""
        SELECT 
            id,
            merchant_id,
            agent_id,
            amount,
            currency,
            status,
            payout_reference,
            file_url,
            method,
            provider,
            external_id,
            period_start,
            period_end,
            metadata,
            uploaded_at,
            confirmed_at,
            created_at,
            updated_at
        FROM agent_payouts
        WHERE {where_clause}
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
        """
        
        result = await database.fetch_all(query=query, values=params)
        return [dict(r) for r in result]
    
    async def get_by_id(self, payout_id: int) -> Optional[Dict[str, Any]]:
        """Get a single payout by ID"""
        query = """
        SELECT * FROM agent_payouts WHERE id = :id
        """
        result = await database.fetch_one(query=query, values={"id": payout_id})
        return dict(result) if result else None
    
    async def create_bulk(self, merchant_id: str, rows: List[dict]) -> List[int]:
        """
        Create multiple payouts at once
        
        Args:
            merchant_id: Merchant creating the payouts
            rows: List of payout data containing agent_id, amount, currency, period_start, period_end
            
        Returns:
            List of created payout IDs
        """
        ids = []
        for row in rows:
            result = await database.fetch_one(
                query="""
                INSERT INTO agent_payouts
                (merchant_id, agent_id, amount, currency, status, period_start, period_end, metadata)
                VALUES (:mid, :aid, :amt, COALESCE(:ccy,'USD'), 'pending', :p0, :p1, COALESCE(:meta, '{}'))
                RETURNING id
                """,
                values={
                    "mid": merchant_id, 
                    "aid": row["agent_id"], 
                    "amt": row["amount"],
                    "ccy": row.get("currency"), 
                    "p0": row.get("period_start"), 
                    "p1": row.get("period_end"),
                    "meta": row.get("metadata")
                }
            )
            if result:
                ids.append(result["id"])
        return ids
    
    async def upload(self, payout_id: int, reference: str, file_url: Optional[str] = None, 
                     method: Optional[str] = None, provider: Optional[str] = None, 
                     external_id: Optional[str] = None) -> bool:
        """
        Mark a payout as uploaded with payment details
        
        Args:
            payout_id: ID of the payout
            reference: Payment reference number
            file_url: URL to payment proof
            method: Payment method (wire, ach, paypal, etc)
            provider: Payment provider name
            external_id: External transaction ID
            
        Returns:
            True if successful
        """
        query = """
        UPDATE agent_payouts SET
          status = 'uploaded',
          payout_reference = :ref,
          file_url = :file,
          method = :method,
          provider = :provider,
          external_id = :eid,
          uploaded_at = NOW(),
          updated_at = NOW()
        WHERE id = :pid AND status = 'pending'
        """
        
        await database.execute(
            query=query,
            values={
                "ref": reference, 
                "file": file_url, 
                "method": method, 
                "provider": provider, 
                "eid": external_id, 
                "pid": payout_id
            }
        )
        return True
    
    async def confirm(self, payout_id: int) -> bool:
        """
        Confirm a payout as paid
        
        Args:
            payout_id: ID of the payout to confirm
            
        Returns:
            True if successful
        """
        query = """
        UPDATE agent_payouts SET 
          status = 'paid',
          confirmed_at = NOW(),
          updated_at = NOW()
        WHERE id = :pid AND status = 'uploaded'
        """
        
        await database.execute(query=query, values={"pid": payout_id})
        return True
    
    async def confirm_bulk(self, payout_ids: List[int]) -> int:
        """
        Confirm multiple payouts as paid
        
        Args:
            payout_ids: List of payout IDs to confirm
            
        Returns:
            Number of payouts confirmed
        """
        if not payout_ids:
            return 0
        
        # Use individual updates for accurate count
        count = 0
        for pid in payout_ids:
            result = await database.fetch_one(
                query="""
                UPDATE agent_payouts 
                SET status = 'paid', confirmed_at = NOW(), updated_at = NOW()
                WHERE id = :pid AND status = 'uploaded'
                RETURNING id
                """,
                values={"pid": pid}
            )
            if result:
                count += 1
        
        return count
    
    async def get_summary_by_merchant(self, merchant_id: str, status: Optional[str] = None) -> Dict[str, Any]:
        """Get payout summary statistics for a merchant"""
        base_condition = "merchant_id = :mid"
        params = {"mid": merchant_id}
        
        if status:
            base_condition += " AND status = :st"
            params["st"] = status
        
        query = f"""
        SELECT 
            COUNT(*) as total_count,
            SUM(amount) as total_amount,
            COUNT(DISTINCT agent_id) as unique_agents,
            MIN(created_at) as first_payout,
            MAX(created_at) as last_payout
        FROM agent_payouts
        WHERE {base_condition}
        """
        
        result = await database.fetch_one(query=query, values=params)
        return dict(result) if result else {
            "total_count": 0,
            "total_amount": 0,
            "unique_agents": 0,
            "first_payout": None,
            "last_payout": None
        }
    
    async def get_summary_by_agent(self, agent_id: str, status: Optional[str] = None) -> Dict[str, Any]:
        """Get payout summary statistics for an agent"""
        base_condition = "agent_id = :aid"
        params = {"aid": agent_id}
        
        if status:
            base_condition += " AND status = :st"
            params["st"] = status
        
        query = f"""
        SELECT 
            COUNT(*) as total_count,
            SUM(amount) as total_amount,
            COUNT(DISTINCT merchant_id) as unique_merchants,
            MIN(confirmed_at) as first_payment,
            MAX(confirmed_at) as last_payment
        FROM agent_payouts
        WHERE {base_condition}
        """
        
        result = await database.fetch_one(query=query, values=params)
        return dict(result) if result else {
            "total_count": 0,
            "total_amount": 0,
            "unique_merchants": 0,
            "first_payment": None,
            "last_payment": None
        }
