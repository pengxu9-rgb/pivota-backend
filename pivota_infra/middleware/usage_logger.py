"""
Middleware to log all Agent API usage to agent_usage_logs table
"""
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from datetime import datetime
import time
from db.database import database

class UsageLoggerMiddleware(BaseHTTPMiddleware):
    """Log Agent API usage for analytics"""
    
    async def dispatch(self, request: Request, call_next):
        # Only log agent API calls
        if not request.url.path.startswith("/agent/v1"):
            return await call_next(request)
        
        # Extract agent info from headers
        api_key = request.headers.get("x-api-key", "")
        agent_id = None
        
        # Try to get agent_id from API key
        if api_key:
            try:
                agent = await database.fetch_one(
                    "SELECT agent_id FROM agents WHERE api_key = :api_key LIMIT 1",
                    {"api_key": api_key}
                )
                if agent:
                    agent_id = agent["agent_id"]
            except:
                pass
        
        # Record start time
        start_time = time.time()
        
        # Process request
        response = await call_next(request)
        
        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)
        
        # Log to database (fire and forget - don't block response)
        if agent_id:
            try:
                await database.execute(
                    """
                    INSERT INTO agent_usage_logs 
                    (agent_id, endpoint, method, status_code, response_time_ms, timestamp, request_id)
                    VALUES (:agent_id, :endpoint, :method, :status_code, :response_time, :timestamp, :request_id)
                    """,
                    {
                        "agent_id": agent_id,
                        "endpoint": request.url.path,
                        "method": request.method,
                        "status_code": response.status_code,
                        "response_time": response_time_ms,
                        "timestamp": datetime.now(),
                        "request_id": request.headers.get("x-request-id", "")
                    }
                )
            except Exception as e:
                # Don't fail the request if logging fails
                print(f"Failed to log usage: {e}")
        
        return response


