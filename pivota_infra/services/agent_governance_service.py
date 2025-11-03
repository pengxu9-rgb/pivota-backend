"""
Agent Governance Service
Handles semi-automatic governance actions with human approval
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from db.database import database

logger = logging.getLogger(__name__)


async def propose_action(
    agent_id: str,
    action_type: str,
    reason: str,
    action_payload: Optional[Dict[str, Any]] = None,
    triggered_by: str = "auto"
) -> Optional[str]:
    """
    Propose a governance action (requires human approval)
    
    Args:
        agent_id: Agent ID
        action_type: reduce_rate_limit, suspend_agent, require_key_rotation, data_quality_warning
        reason: Why this action is proposed
        action_payload: Action-specific parameters
        triggered_by: auto or manual
    
    Returns:
        action_id if created
    """
    try:
        # Check if similar pending action exists
        existing = await database.fetch_one(
            """SELECT action_id FROM governance_actions_log
               WHERE agent_id = :agent_id
                 AND action_type = :action_type
                 AND status = 'pending'
                 AND created_at >= NOW() - INTERVAL '1 hour'""",
            {"agent_id": agent_id, "action_type": action_type}
        )
        
        if existing:
            logger.debug(f"Governance action already pending for agent {agent_id}, type {action_type}")
            return dict(existing).get("action_id")
        
        # Create new action proposal
        action_id = f"action_{uuid.uuid4().hex[:16]}"
        
        await database.execute(
            """INSERT INTO governance_actions_log
               (action_id, agent_id, action_type, triggered_by, action_payload, 
                status, reason, created_at)
               VALUES (:action_id, :agent_id, :action_type, :triggered_by, :action_payload,
                       :status, :reason, :created_at)""",
            {
                "action_id": action_id,
                "agent_id": agent_id,
                "action_type": action_type,
                "triggered_by": triggered_by,
                "action_payload": str(action_payload) if action_payload else None,
                "status": "pending",
                "reason": reason,
                "created_at": datetime.now()
            }
        )
        
        logger.info(f"📋 Governance action proposed: {action_id} for agent {agent_id} ({action_type})")
        
        return action_id
        
    except Exception as e:
        logger.error(f"Failed to propose governance action for agent {agent_id}: {e}")
        return None


async def execute_governance_action(
    action_id: str,
    executor_email: str
) -> bool:
    """
    Execute an approved governance action
    
    Args:
        action_id: Action ID
        executor_email: Email of person executing
    
    Returns:
        True if successful
    """
    try:
        # Get action details
        action = await database.fetch_one(
            "SELECT * FROM governance_actions_log WHERE action_id = :action_id",
            {"action_id": action_id}
        )
        
        if not action:
            logger.error(f"Action {action_id} not found")
            return False
        
        action_dict = dict(action)
        agent_id = action_dict.get("agent_id")
        action_type = action_dict.get("action_type")
        action_payload = action_dict.get("action_payload")
        
        # Parse payload
        import json
        payload = {}
        if action_payload:
            try:
                payload = json.loads(action_payload) if isinstance(action_payload, str) else action_payload
            except:
                payload = {}
        
        # Execute based on action type
        if action_type == "reduce_rate_limit":
            new_limit = payload.get("new_limit", 50)
            await database.execute(
                "UPDATE agents SET rate_limit = :new_limit WHERE agent_id = :agent_id",
                {"new_limit": new_limit, "agent_id": agent_id}
            )
            await database.execute(
                "UPDATE agent_policies SET max_requests_per_minute = :new_limit WHERE agent_id = :agent_id",
                {"new_limit": new_limit, "agent_id": agent_id}
            )
            logger.info(f"✅ Reduced rate limit for agent {agent_id} to {new_limit}")
        
        elif action_type == "suspend_agent":
            duration_hours = payload.get("duration_hours", 24)
            await database.execute(
                "UPDATE agents SET status = 'suspended', deactivated_at = :now WHERE agent_id = :agent_id",
                {"now": datetime.now(), "agent_id": agent_id}
            )
            logger.info(f"✅ Suspended agent {agent_id} for {duration_hours} hours")
        
        elif action_type == "require_key_rotation":
            # Create an alert for manual key rotation
            from services.agent_anomaly_detector import create_alert
            deadline = payload.get("deadline", (datetime.now() + timedelta(days=7)).isoformat())
            await create_alert(
                agent_id,
                "key_rotation_required",
                "warning",
                f"API key rotation required by {deadline}",
                {"deadline": deadline}
            )
            logger.info(f"✅ Created key rotation requirement for agent {agent_id}")
        
        elif action_type == "data_quality_warning":
            # Log warning only, no action
            logger.warning(f"⚠️ Data quality warning for agent {agent_id}: {payload.get('message')}")
        
        # Update action status
        await database.execute(
            """UPDATE governance_actions_log
               SET status = 'executed', executed_by = :executor, executed_at = :executed_at
               WHERE action_id = :action_id""",
            {
                "executor": executor_email,
                "executed_at": datetime.now(),
                "action_id": action_id
            }
        )
        
        logger.info(f"✅ Governance action {action_id} executed by {executor_email}")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to execute governance action {action_id}: {e}")
        
        # Mark as failed
        await database.execute(
            "UPDATE governance_actions_log SET status = 'failed' WHERE action_id = :action_id",
            {"action_id": action_id}
        )
        
        return False


async def reject_governance_action(
    action_id: str,
    executor_email: str,
    rejection_reason: str
) -> bool:
    """
    Reject a governance action
    
    Args:
        action_id: Action ID
        executor_email: Email of person rejecting
        rejection_reason: Why rejected
    
    Returns:
        True if successful
    """
    try:
        await database.execute(
            """UPDATE governance_actions_log
               SET status = 'rejected', executed_by = :executor, executed_at = :executed_at, reason = :reason
               WHERE action_id = :action_id""",
            {
                "executor": executor_email,
                "executed_at": datetime.now(),
                "reason": rejection_reason,
                "action_id": action_id
            }
        )
        
        logger.info(f"❌ Governance action {action_id} rejected by {executor_email}: {rejection_reason}")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to reject governance action {action_id}: {e}")
        return False


async def get_pending_actions() -> List[Dict[str, Any]]:
    """
    Get all pending governance actions
    
    Returns:
        List of pending actions with agent info
    """
    try:
        actions = await database.fetch_all(
            """SELECT 
                g.action_id, g.agent_id, g.action_type, g.triggered_by,
                g.action_payload, g.reason, g.created_at,
                a.name as agent_name, a.email as agent_email
               FROM governance_actions_log g
               JOIN agents a ON g.agent_id = a.agent_id
               WHERE g.status = 'pending'
               ORDER BY g.created_at DESC"""
        )
        
        return [dict(a) for a in actions]
        
    except Exception as e:
        logger.error(f"Failed to get pending actions: {e}")
        return []


async def get_governance_history(
    agent_id: str,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Get governance action history for an agent
    
    Args:
        agent_id: Agent ID
        limit: Max number of records
    
    Returns:
        List of actions
    """
    try:
        actions = await database.fetch_all(
            """SELECT action_id, action_type, triggered_by, executed_by,
                      action_payload, status, reason, created_at, executed_at
               FROM governance_actions_log
               WHERE agent_id = :agent_id
               ORDER BY created_at DESC
               LIMIT :limit""",
            {"agent_id": agent_id, "limit": limit}
        )
        
        return [dict(a) for a in actions]
        
    except Exception as e:
        logger.error(f"Failed to get governance history for agent {agent_id}: {e}")
        return []

Agent Governance Service
Handles semi-automatic governance actions with human approval
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from db.database import database

logger = logging.getLogger(__name__)


async def propose_action(
    agent_id: str,
    action_type: str,
    reason: str,
    action_payload: Optional[Dict[str, Any]] = None,
    triggered_by: str = "auto"
) -> Optional[str]:
    """
    Propose a governance action (requires human approval)
    
    Args:
        agent_id: Agent ID
        action_type: reduce_rate_limit, suspend_agent, require_key_rotation, data_quality_warning
        reason: Why this action is proposed
        action_payload: Action-specific parameters
        triggered_by: auto or manual
    
    Returns:
        action_id if created
    """
    try:
        # Check if similar pending action exists
        existing = await database.fetch_one(
            """SELECT action_id FROM governance_actions_log
               WHERE agent_id = :agent_id
                 AND action_type = :action_type
                 AND status = 'pending'
                 AND created_at >= NOW() - INTERVAL '1 hour'""",
            {"agent_id": agent_id, "action_type": action_type}
        )
        
        if existing:
            logger.debug(f"Governance action already pending for agent {agent_id}, type {action_type}")
            return dict(existing).get("action_id")
        
        # Create new action proposal
        action_id = f"action_{uuid.uuid4().hex[:16]}"
        
        await database.execute(
            """INSERT INTO governance_actions_log
               (action_id, agent_id, action_type, triggered_by, action_payload, 
                status, reason, created_at)
               VALUES (:action_id, :agent_id, :action_type, :triggered_by, :action_payload,
                       :status, :reason, :created_at)""",
            {
                "action_id": action_id,
                "agent_id": agent_id,
                "action_type": action_type,
                "triggered_by": triggered_by,
                "action_payload": str(action_payload) if action_payload else None,
                "status": "pending",
                "reason": reason,
                "created_at": datetime.now()
            }
        )
        
        logger.info(f"📋 Governance action proposed: {action_id} for agent {agent_id} ({action_type})")
        
        return action_id
        
    except Exception as e:
        logger.error(f"Failed to propose governance action for agent {agent_id}: {e}")
        return None


async def execute_governance_action(
    action_id: str,
    executor_email: str
) -> bool:
    """
    Execute an approved governance action
    
    Args:
        action_id: Action ID
        executor_email: Email of person executing
    
    Returns:
        True if successful
    """
    try:
        # Get action details
        action = await database.fetch_one(
            "SELECT * FROM governance_actions_log WHERE action_id = :action_id",
            {"action_id": action_id}
        )
        
        if not action:
            logger.error(f"Action {action_id} not found")
            return False
        
        action_dict = dict(action)
        agent_id = action_dict.get("agent_id")
        action_type = action_dict.get("action_type")
        action_payload = action_dict.get("action_payload")
        
        # Parse payload
        import json
        payload = {}
        if action_payload:
            try:
                payload = json.loads(action_payload) if isinstance(action_payload, str) else action_payload
            except:
                payload = {}
        
        # Execute based on action type
        if action_type == "reduce_rate_limit":
            new_limit = payload.get("new_limit", 50)
            await database.execute(
                "UPDATE agents SET rate_limit = :new_limit WHERE agent_id = :agent_id",
                {"new_limit": new_limit, "agent_id": agent_id}
            )
            await database.execute(
                "UPDATE agent_policies SET max_requests_per_minute = :new_limit WHERE agent_id = :agent_id",
                {"new_limit": new_limit, "agent_id": agent_id}
            )
            logger.info(f"✅ Reduced rate limit for agent {agent_id} to {new_limit}")
        
        elif action_type == "suspend_agent":
            duration_hours = payload.get("duration_hours", 24)
            await database.execute(
                "UPDATE agents SET status = 'suspended', deactivated_at = :now WHERE agent_id = :agent_id",
                {"now": datetime.now(), "agent_id": agent_id}
            )
            logger.info(f"✅ Suspended agent {agent_id} for {duration_hours} hours")
        
        elif action_type == "require_key_rotation":
            # Create an alert for manual key rotation
            from services.agent_anomaly_detector import create_alert
            deadline = payload.get("deadline", (datetime.now() + timedelta(days=7)).isoformat())
            await create_alert(
                agent_id,
                "key_rotation_required",
                "warning",
                f"API key rotation required by {deadline}",
                {"deadline": deadline}
            )
            logger.info(f"✅ Created key rotation requirement for agent {agent_id}")
        
        elif action_type == "data_quality_warning":
            # Log warning only, no action
            logger.warning(f"⚠️ Data quality warning for agent {agent_id}: {payload.get('message')}")
        
        # Update action status
        await database.execute(
            """UPDATE governance_actions_log
               SET status = 'executed', executed_by = :executor, executed_at = :executed_at
               WHERE action_id = :action_id""",
            {
                "executor": executor_email,
                "executed_at": datetime.now(),
                "action_id": action_id
            }
        )
        
        logger.info(f"✅ Governance action {action_id} executed by {executor_email}")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to execute governance action {action_id}: {e}")
        
        # Mark as failed
        await database.execute(
            "UPDATE governance_actions_log SET status = 'failed' WHERE action_id = :action_id",
            {"action_id": action_id}
        )
        
        return False


async def reject_governance_action(
    action_id: str,
    executor_email: str,
    rejection_reason: str
) -> bool:
    """
    Reject a governance action
    
    Args:
        action_id: Action ID
        executor_email: Email of person rejecting
        rejection_reason: Why rejected
    
    Returns:
        True if successful
    """
    try:
        await database.execute(
            """UPDATE governance_actions_log
               SET status = 'rejected', executed_by = :executor, executed_at = :executed_at, reason = :reason
               WHERE action_id = :action_id""",
            {
                "executor": executor_email,
                "executed_at": datetime.now(),
                "reason": rejection_reason,
                "action_id": action_id
            }
        )
        
        logger.info(f"❌ Governance action {action_id} rejected by {executor_email}: {rejection_reason}")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to reject governance action {action_id}: {e}")
        return False


async def get_pending_actions() -> List[Dict[str, Any]]:
    """
    Get all pending governance actions
    
    Returns:
        List of pending actions with agent info
    """
    try:
        actions = await database.fetch_all(
            """SELECT 
                g.action_id, g.agent_id, g.action_type, g.triggered_by,
                g.action_payload, g.reason, g.created_at,
                a.name as agent_name, a.email as agent_email
               FROM governance_actions_log g
               JOIN agents a ON g.agent_id = a.agent_id
               WHERE g.status = 'pending'
               ORDER BY g.created_at DESC"""
        )
        
        return [dict(a) for a in actions]
        
    except Exception as e:
        logger.error(f"Failed to get pending actions: {e}")
        return []


async def get_governance_history(
    agent_id: str,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Get governance action history for an agent
    
    Args:
        agent_id: Agent ID
        limit: Max number of records
    
    Returns:
        List of actions
    """
    try:
        actions = await database.fetch_all(
            """SELECT action_id, action_type, triggered_by, executed_by,
                      action_payload, status, reason, created_at, executed_at
               FROM governance_actions_log
               WHERE agent_id = :agent_id
               ORDER BY created_at DESC
               LIMIT :limit""",
            {"agent_id": agent_id, "limit": limit}
        )
        
        return [dict(a) for a in actions]
        
    except Exception as e:
        logger.error(f"Failed to get governance history for agent {agent_id}: {e}")
        return []

