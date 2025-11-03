"""
[Phase 5] Agent Revenue Settlement Job
Scheduled background task for processing agent revenue settlements
"""

from datetime import datetime, timedelta
from decimal import Decimal
import asyncio
import logging
from typing import List, Dict, Any
import json

from db.database import database

logger = logging.getLogger(__name__)


class AgentRevenueSettlementJob:
    """Background job for processing agent revenue settlements"""
    
    def __init__(self, database_instance):
        self.database = database_instance
        self.batch_id_prefix = "batch"
        
    async def run_daily_settlement(self):
        """
        Daily settlement: Calculate pending earnings
        Run at 00:00 UTC
        """
        logger.info("[Phase 5] Running daily settlement job")
        
        try:
            # Get all pending revenue logs
            pending_logs = await self.database.fetch_all(
                """
                SELECT 
                    agent_id,
                    currency,
                    COUNT(*) as transaction_count,
                    SUM(agent_earned_amount) as total_amount
                FROM agent_revenue_logs
                WHERE settlement_status = 'pending'
                AND created_at < NOW() - INTERVAL '24 hours'
                GROUP BY agent_id, currency
                """
            )
            
            summary = []
            for record in pending_logs:
                summary.append({
                    "agent_id": record["agent_id"],
                    "currency": record["currency"],
                    "transactions": record["transaction_count"],
                    "amount": float(record["total_amount"])
                })
            
            logger.info(f"[Phase 5] Daily settlement summary: {len(summary)} agents with pending earnings")
            
            return {
                "status": "completed",
                "job_type": "daily",
                "timestamp": datetime.utcnow().isoformat(),
                "summary": summary
            }
            
        except Exception as e:
            logger.error(f"[Phase 5] Daily settlement failed: {e}")
            return {
                "status": "failed",
                "error": str(e)
            }
    
    async def run_weekly_settlement(self):
        """
        Weekly settlement: Generate settlement batches
        Run every Monday at 00:00 UTC
        """
        logger.info("[Phase 5] Running weekly settlement job")
        
        try:
            # Generate batch ID
            batch_id = f"{self.batch_id_prefix}_{datetime.utcnow().strftime('%Y%m%d')}"
            
            # Get agents with pending revenue
            agents_to_settle = await self.database.fetch_all(
                """
                SELECT DISTINCT agent_id, currency
                FROM agent_revenue_logs
                WHERE settlement_status = 'pending'
                AND created_at < NOW() - INTERVAL '7 days'
                """
            )
            
            settled_count = 0
            
            for agent_record in agents_to_settle:
                agent_id = agent_record["agent_id"]
                currency = agent_record["currency"]
                
                # Mark logs for this batch
                result = await self.database.execute(
                    """
                    UPDATE agent_revenue_logs
                    SET settlement_status = 'processing',
                        settlement_batch_id = :batch_id
                    WHERE agent_id = :agent_id
                    AND currency = :currency
                    AND settlement_status = 'pending'
                    AND created_at < NOW() - INTERVAL '7 days'
                    """,
                    {
                        "batch_id": batch_id,
                        "agent_id": agent_id,
                        "currency": currency
                    }
                )
                
                settled_count += result
            
            logger.info(f"[Phase 5] Weekly settlement: batch={batch_id}, logs={settled_count}")
            
            return {
                "status": "completed",
                "job_type": "weekly",
                "batch_id": batch_id,
                "logs_processed": settled_count,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"[Phase 5] Weekly settlement failed: {e}")
            return {
                "status": "failed",
                "error": str(e)
            }
    
    async def run_monthly_analytics(self):
        """
        Monthly settlement: Update revenue analytics
        Run on 1st of each month at 00:00 UTC
        """
        logger.info("[Phase 5] Running monthly analytics job")
        
        try:
            # Get monthly summary per agent
            monthly_summary = await self.database.fetch_all(
                """
                SELECT 
                    agent_id,
                    currency,
                    COUNT(*) as transactions,
                    SUM(agent_earned_amount) as total_earned,
                    AVG(split_ratio_applied) as avg_ratio,
                    COUNT(DISTINCT merchant_id) as unique_merchants
                FROM agent_revenue_logs
                WHERE created_at >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '1 month')
                AND created_at < DATE_TRUNC('month', CURRENT_DATE)
                GROUP BY agent_id, currency
                """
            )
            
            analytics = []
            for record in monthly_summary:
                analytics.append({
                    "agent_id": record["agent_id"],
                    "currency": record["currency"],
                    "transactions": record["transactions"],
                    "total_earned": float(record["total_earned"]),
                    "avg_split_ratio": float(record["avg_ratio"]) if record["avg_ratio"] else 0,
                    "unique_merchants": record["unique_merchants"]
                })
            
            logger.info(f"[Phase 5] Monthly analytics: {len(analytics)} agents processed")
            
            return {
                "status": "completed",
                "job_type": "monthly",
                "analytics": analytics,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"[Phase 5] Monthly analytics failed: {e}")
            return {
                "status": "failed",
                "error": str(e)
            }
    
    async def mark_batch_settled(self, batch_id: str):
        """
        Mark a settlement batch as completed
        Called after actual payout is processed
        """
        try:
            result = await self.database.execute(
                """
                UPDATE agent_revenue_logs
                SET settlement_status = 'settled',
                    settled_at = NOW()
                WHERE settlement_batch_id = :batch_id
                AND settlement_status = 'processing'
                """,
                {"batch_id": batch_id}
            )
            
            logger.info(f"[Phase 5] Batch {batch_id} marked as settled: {result} logs")
            
            return result
            
        except Exception as e:
            logger.error(f"[Phase 5] Failed to mark batch settled: {e}")
            raise


# [Phase 5] Scheduler setup (example using APScheduler)
async def schedule_settlement_jobs():
    """
    Setup scheduled jobs for revenue settlement
    
    Usage with APScheduler:
    
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    
    scheduler = AsyncIOScheduler()
    job = AgentRevenueSettlementJob(database)
    
    # Daily at 00:00 UTC
    scheduler.add_job(job.run_daily_settlement, 'cron', hour=0, minute=0)
    
    # Weekly on Monday at 00:00 UTC
    scheduler.add_job(job.run_weekly_settlement, 'cron', day_of_week='mon', hour=0, minute=0)
    
    # Monthly on 1st at 00:00 UTC
    scheduler.add_job(job.run_monthly_analytics, 'cron', day=1, hour=0, minute=0)
    
    scheduler.start()
    """
    logger.info("[Phase 5] Settlement job scheduler ready")


# [Phase 5] Test if module loads correctly
if __name__ == "__main__":
    print("[Phase 5] Agent Revenue Settlement Job module loaded")
    print("Scheduled tasks:")
    print("  - Daily 00:00 UTC: Calculate pending earnings")
    print("  - Weekly Monday 00:00 UTC: Generate settlement batches")
    print("  - Monthly 1st 00:00 UTC: Update revenue analytics")
