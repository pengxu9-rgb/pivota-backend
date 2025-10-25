"""Payout Routes for Merchant Dashboard"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import random
import string
from utils.auth import get_current_user

router = APIRouter(prefix="/merchant/payouts", tags=["payouts"])

def generate_payout_id() -> str:
    """Generate a unique payout ID"""
    return "payout_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))

def generate_demo_payouts(merchant_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Generate demo payout data"""
    payouts = []
    statuses = ["completed", "completed", "pending", "processing", "failed"]
    
    for i in range(limit):
        payout_date = datetime.now() - timedelta(days=random.randint(0, 90))
        arrival_date = payout_date + timedelta(days=random.randint(1, 3))
        
        payouts.append({
            "id": generate_payout_id(),
            "merchant_id": merchant_id,
            "amount": round(random.uniform(500, 5000), 2),
            "currency": "USD",
            "status": random.choice(statuses),
            "description": f"Payout for period {payout_date.strftime('%Y-%m-%d')}",
            "created_at": payout_date.isoformat() + "Z",
            "arrival_date": arrival_date.isoformat() + "Z" if random.choice([True, False]) else None,
            "bank_account_last4": str(random.randint(1000, 9999)),
            "failure_message": "Insufficient funds" if random.random() < 0.1 else None
        })
    
    return sorted(payouts, key=lambda x: x["created_at"], reverse=True)

@router.get("/")
async def get_payouts(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant payouts"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = "merch_208139f7600dbf42"  # Demo merchant ID
    payouts = generate_demo_payouts(merchant_id, limit=50)
    
    # Filter by status if provided
    if status:
        payouts = [p for p in payouts if p["status"] == status]
    
    # Apply pagination
    total = len(payouts)
    payouts = payouts[offset:offset + limit]
    
    return {
        "status": "success",
        "data": {
            "payouts": payouts,
            "total": total,
            "limit": limit,
            "offset": offset
        }
    }

@router.get("/stats")
async def get_payout_stats(current_user: dict = Depends(get_current_user)):
    """Get payout statistics"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = "merch_208139f7600dbf42"
    payouts = generate_demo_payouts(merchant_id, limit=100)
    
    # Calculate statistics
    total_paid = sum(p["amount"] for p in payouts if p["status"] == "completed")
    pending_amount = sum(p["amount"] for p in payouts if p["status"] in ["pending", "processing"])
    failed_amount = sum(p["amount"] for p in payouts if p["status"] == "failed")
    
    completed_payouts = [p for p in payouts if p["status"] == "completed"]
    avg_payout = total_paid / len(completed_payouts) if completed_payouts else 0
    
    # Monthly breakdown
    monthly_payouts = {}
    for payout in completed_payouts:
        month = payout["created_at"][:7]  # YYYY-MM
        if month not in monthly_payouts:
            monthly_payouts[month] = 0
        monthly_payouts[month] += payout["amount"]
    
    monthly_data = [
        {"month": month, "amount": amount}
        for month, amount in sorted(monthly_payouts.items())
    ]
    
    return {
        "status": "success",
        "data": {
            "total_paid": round(total_paid, 2),
            "pending_amount": round(pending_amount, 2),
            "failed_amount": round(failed_amount, 2),
            "average_payout": round(avg_payout, 2),
            "total_payouts": len(completed_payouts),
            "monthly_breakdown": monthly_data[-12:]  # Last 12 months
        }
    }

@router.get("/{payout_id}")
async def get_payout_details(
    payout_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get payout details"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Generate a single payout for demo
    payout_date = datetime.now() - timedelta(days=random.randint(0, 30))
    arrival_date = payout_date + timedelta(days=2)
    
    payout = {
        "id": payout_id,
        "merchant_id": "merch_208139f7600dbf42",
        "amount": round(random.uniform(1000, 5000), 2),
        "currency": "USD",
        "status": random.choice(["completed", "pending", "processing"]),
        "description": f"Payout for period {payout_date.strftime('%Y-%m-%d')}",
        "created_at": payout_date.isoformat() + "Z",
        "arrival_date": arrival_date.isoformat() + "Z",
        "bank_account": {
            "last4": str(random.randint(1000, 9999)),
            "bank_name": "Chase Bank",
            "account_type": "checking"
        },
        "transactions": [
            {
                "order_id": f"ORD{random.randint(10000, 99999)}",
                "amount": round(random.uniform(50, 500), 2),
                "fee": round(random.uniform(1, 15), 2),
                "net": round(random.uniform(45, 490), 2)
            }
            for _ in range(random.randint(5, 15))
        ]
    }
    
    return {
        "status": "success",
        "data": payout
    }

@router.post("/request")
async def request_payout(
    current_user: dict = Depends(get_current_user)
):
    """Request a manual payout"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # In real implementation, check available balance and create payout request
    return {
        "status": "success",
        "message": "Payout requested successfully. Processing time: 1-3 business days.",
        "data": {
            "payout_id": generate_payout_id(),
            "status": "pending",
            "created_at": datetime.now().isoformat() + "Z"
        }
    }








