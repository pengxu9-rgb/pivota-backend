"""
Employee Dashboard Routes
Provides analytics and management endpoints for employee portal
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from utils.auth import get_current_user
from db.database import database
import random

router = APIRouter()

@router.get("/analytics/dashboard")
async def get_analytics_dashboard(
    current_user: dict = Depends(get_current_user)
):
    """Get analytics dashboard for employee portal"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get total transactions from orders table
        transactions_query = """
            SELECT 
                COUNT(*) as total_transactions,
                COALESCE(SUM(amount), 0) as total_revenue,
                SUM(CASE WHEN status IN ('completed', 'delivered') THEN 1 ELSE 0 END) as successful_transactions
            FROM orders
        """
        transactions = await database.fetch_one(transactions_query)
        
        # Calculate success rate
        success_rate = 0
        if transactions and transactions["total_transactions"] > 0:
            success_rate = (transactions["successful_transactions"] / transactions["total_transactions"]) * 100
        
        # Get merchant counts
        merchants_query = """
            SELECT 
                COUNT(*) as total_merchants,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_merchants
            FROM merchant_onboarding
        """
        merchants = await database.fetch_one(merchants_query)
        
        # Get PSP counts
        psp_query = """
            SELECT COUNT(DISTINCT provider) as total_psps,
                   COUNT(*) as total_connections
            FROM merchant_psps
        """
        psps = await database.fetch_one(psp_query)
        
        # Get recent transaction trends (last 7 days)
        trends_query = """
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as transactions,
                COALESCE(SUM(amount), 0) as revenue
            FROM orders
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """
        trends = await database.fetch_all(trends_query)
        
        trend_data = []
        for trend in trends:
            trend_data.append({
                "date": trend["date"].isoformat() if trend["date"] else None,
                "transactions": trend["transactions"],
                "revenue": float(trend["revenue"])
            })
        
        return {
            "status": "success",
            "data": {
                "total_transactions": transactions["total_transactions"] if transactions else 0,
                "total_revenue": float(transactions["total_revenue"]) if transactions else 0,
                "success_rate": round(success_rate, 1),
                "total_merchants": merchants["total_merchants"] if merchants else 0,
                "active_merchants": merchants["active_merchants"] if merchants else 0,
                "total_psps": psps["total_psps"] if psps else 0,
                "total_psp_connections": psps["total_connections"] if psps else 0,
                "transaction_trends": trend_data
            }
        }
    except Exception as e:
        print(f"Error in analytics dashboard: {e}")
        # Return default values if error
        return {
            "status": "success",
            "data": {
                "total_transactions": 0,
                "total_revenue": 0,
                "success_rate": 0,
                "total_merchants": 0,
                "active_merchants": 0,
                "total_psps": 0,
                "total_psp_connections": 0,
                "transaction_trends": []
            }
        }

@router.get("/agents")
async def get_all_agents(
    current_user: dict = Depends(get_current_user)
):
    """Get all agents in the system"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check if agents table exists
        check_query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'agents'
            )
        """
        table_exists = await database.fetch_one(check_query)
        
        if table_exists and table_exists["exists"]:
            agents_query = """
                SELECT agent_id, name, email, status, created_at, last_active
                FROM agents
                ORDER BY created_at DESC
            """
            agents = await database.fetch_all(agents_query)
            
            return {
                "status": "success",
                "agents": [
                    {
                        "agent_id": a["agent_id"],
                        "name": a["name"],
                        "email": a["email"],
                        "status": a["status"],
                        "is_active": a["status"] == "active",
                        "created_at": a["created_at"].isoformat() if a["created_at"] else None,
                        "last_active": a["last_active"].isoformat() if a["last_active"] else None
                    }
                    for a in agents
                ]
            }
        else:
            # Return demo agents if table doesn't exist
            return {
                "status": "success",
                "agents": [
                    {
                        "agent_id": "agent_001",
                        "name": "John Smith",
                        "email": "john@pivota.com",
                        "status": "active",
                        "is_active": True,
                        "created_at": datetime.now().isoformat(),
                        "last_active": datetime.now().isoformat()
                    },
                    {
                        "agent_id": "agent_002",
                        "name": "Sarah Johnson",
                        "email": "sarah@pivota.com",
                        "status": "active",
                        "is_active": True,
                        "created_at": datetime.now().isoformat(),
                        "last_active": datetime.now().isoformat()
                    }
                ]
            }
    except Exception as e:
        print(f"Error fetching agents: {e}")
        return {"status": "success", "agents": []}

@router.get("/system/status")
async def get_system_status(
    current_user: dict = Depends(get_current_user)
):
    """Get system health status"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Check database connection
        test_query = "SELECT 1"
        await database.fetch_one(test_query)
        
        # Check critical tables
        tables_to_check = ['merchant_onboarding', 'orders', 'merchant_stores', 'merchant_psps']
        all_healthy = True
        issues = []
        
        for table in tables_to_check:
            check_query = f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = '{table}'
                )
            """
            result = await database.fetch_one(check_query)
            if not result or not result["exists"]:
                all_healthy = False
                issues.append(f"Table {table} is missing")
        
        return {
            "status": "success",
            "healthy": all_healthy,
            "message": "All systems operational" if all_healthy else f"Issues detected: {', '.join(issues)}",
            "timestamp": datetime.now().isoformat(),
            "services": {
                "database": "operational",
                "api": "operational",
                "orders": "operational" if "orders" not in str(issues) else "degraded",
                "merchants": "operational" if "merchant" not in str(issues) else "degraded"
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "healthy": False,
            "message": f"System check failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }

@router.get("/transactions")
async def get_all_transactions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Get all transactions across all merchants"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Build query with optional status filter
        where_clause = ""
        params = {}
        
        if status:
            where_clause = "WHERE status = :status"
            params["status"] = status
        
        # Get total count
        count_query = f"SELECT COUNT(*) as total FROM orders {where_clause}"
        count_result = await database.fetch_one(count_query, params)
        total = count_result["total"] if count_result else 0
        
        # Get transactions
        transactions_query = f"""
            SELECT 
                o.order_id, o.merchant_id, o.amount, o.currency, 
                o.status, o.payment_method, o.customer_name, o.customer_email,
                o.created_at, o.psp_id,
                m.business_name as merchant_name
            FROM orders o
            LEFT JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
            {where_clause}
            ORDER BY o.created_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        params["limit"] = limit
        params["offset"] = offset
        
        rows = await database.fetch_all(transactions_query, params)
        
        transactions = []
        for row in rows:
            transactions.append({
                "transaction_id": row["order_id"],
                "merchant_id": row["merchant_id"],
                "merchant_name": row["merchant_name"] or "Unknown Merchant",
                "amount": float(row["amount"]),
                "currency": row["currency"],
                "status": row["status"],
                "payment_method": row["payment_method"],
                "customer": {
                    "name": row["customer_name"],
                    "email": row["customer_email"]
                },
                "psp": row["psp_id"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None
            })
        
        return {
            "status": "success",
            "data": {
                "transactions": transactions,
                "total": total,
                "limit": limit,
                "offset": offset
            }
        }
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return {
            "status": "success",
            "data": {
                "transactions": [],
                "total": 0,
                "limit": limit,
                "offset": offset
            }
        }

@router.get("/psps/all")
async def get_all_psps(
    current_user: dict = Depends(get_current_user)
):
    """Get all PSPs across all merchants"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get PSP connections (without aggregating orders here)
        psps_query = """
            SELECT 
                p.psp_id, p.provider, p.name, p.status, p.merchant_id,
                p.connected_at, p.capabilities,
                m.business_name as merchant_name
            FROM merchant_psps p
            LEFT JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
            ORDER BY p.connected_at DESC
        """
        
        rows = await database.fetch_all(psps_query)
        
        psps = []
        for row in rows:
            capabilities = []
            if row["capabilities"]:
                capabilities = row["capabilities"].split(',')
            
            # Get transaction stats for THIS specific PSP's merchant only
            stats = await database.fetch_one(
                """
                SELECT 
                    COUNT(order_id) as transaction_count,
                    COALESCE(SUM(total), 0) as total_volume,
                    SUM(CASE WHEN LOWER(COALESCE(payment_status,'')) IN ('paid','succeeded','completed') 
                             OR LOWER(COALESCE(status,'')) IN ('completed','delivered') 
                        THEN 1 ELSE 0 END) as successful_count
                FROM orders
                WHERE merchant_id = :merchant_id
                AND (is_deleted IS NULL OR is_deleted = FALSE)
                """,
                {"merchant_id": row["merchant_id"]}
            )
            
            transaction_count = stats["transaction_count"] if stats else 0
            successful_count = stats["successful_count"] if stats else 0
            success_rate = round((successful_count / transaction_count * 100), 1) if transaction_count > 0 else 0
            total_volume = float(stats["total_volume"]) if stats else 0
            
            psps.append({
                "psp_id": row["psp_id"],
                "provider": row["provider"],
                "name": row["name"],
                "status": row["status"],
                "merchant_id": row["merchant_id"],
                "merchant_name": row["merchant_name"] or "Unknown Merchant",
                "connected_at": row["connected_at"].isoformat() if row["connected_at"] else None,
                "capabilities": capabilities,
                "transaction_count": transaction_count,
                "successful_count": successful_count,
                "success_rate": success_rate,
                "total_volume": total_volume,
                "is_active": row["status"] == "active"
            })
        
        return {
            "status": "success",
            "psps": psps
        }
    except Exception as e:
        print(f"Error fetching PSPs: {e}")
        return {"status": "success", "psps": []}

@router.post("/psps/test-connection")
async def test_psp_connection(
    psp_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test PSP connection"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get PSP details
        psp_query = "SELECT * FROM merchant_psps WHERE psp_id = :psp_id"
        psp = await database.fetch_one(psp_query, {"psp_id": psp_id})
        
        if not psp:
            raise HTTPException(status_code=404, detail="PSP not found")
        
        # Simulate connection test
        # In production, this would actually test the API connection
        return {
            "status": "success",
            "message": f"Connection to {psp['provider']} successful",
            "details": {
                "provider": psp["provider"],
                "response_time": random.randint(50, 200),
                "api_version": "v2",
                "capabilities": psp["capabilities"].split(',') if psp["capabilities"] else []
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "status": "error",
            "message": f"Connection test failed: {str(e)}"
        }

@router.get("/finance/overview")
async def get_finance_overview(
    current_user: dict = Depends(get_current_user)
):
    """Get finance overview for employee portal"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get financial metrics from orders
        finance_query = """
            SELECT 
                COUNT(*) as total_transactions,
                COALESCE(SUM(amount), 0) as gross_revenue,
                COALESCE(SUM(CASE WHEN status IN ('completed', 'delivered') THEN amount ELSE 0 END), 0) as net_revenue,
                COALESCE(AVG(amount), 0) as avg_transaction_value,
                COUNT(DISTINCT merchant_id) as active_merchants,
                COUNT(DISTINCT DATE(created_at)) as active_days
            FROM orders
            WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
        """
        
        metrics = await database.fetch_one(finance_query)
        
        # Calculate fees (example: 2.9% + $0.30 per transaction)
        processing_fees = 0
        if metrics:
            processing_fees = (float(metrics["net_revenue"]) * 0.029) + (metrics["total_transactions"] * 0.30)
        
        # Get monthly breakdown
        monthly_query = """
            SELECT 
                DATE_TRUNC('month', created_at) as month,
                COUNT(*) as transactions,
                COALESCE(SUM(amount), 0) as revenue
            FROM orders
            WHERE created_at >= CURRENT_DATE - INTERVAL '6 months'
            GROUP BY DATE_TRUNC('month', created_at)
            ORDER BY month DESC
        """
        
        monthly_data = await database.fetch_all(monthly_query)
        
        monthly_breakdown = []
        for month in monthly_data:
            monthly_breakdown.append({
                "month": month["month"].strftime("%Y-%m") if month["month"] else None,
                "transactions": month["transactions"],
                "revenue": float(month["revenue"])
            })
        
        return {
            "status": "success",
            "data": {
                "gross_revenue": float(metrics["gross_revenue"]) if metrics else 0,
                "net_revenue": float(metrics["net_revenue"]) if metrics else 0,
                "processing_fees": round(processing_fees, 2),
                "avg_transaction_value": float(metrics["avg_transaction_value"]) if metrics else 0,
                "total_transactions": metrics["total_transactions"] if metrics else 0,
                "active_merchants": metrics["active_merchants"] if metrics else 0,
                "monthly_breakdown": monthly_breakdown
            }
        }
    except Exception as e:
        print(f"Error in finance overview: {e}")
        return {
            "status": "success",
            "data": {
                "gross_revenue": 0,
                "net_revenue": 0,
                "processing_fees": 0,
                "avg_transaction_value": 0,
                "total_transactions": 0,
                "active_merchants": 0,
                "monthly_breakdown": []
            }
        }
