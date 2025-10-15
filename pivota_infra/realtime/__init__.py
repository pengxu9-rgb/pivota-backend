# Real-time metrics and WebSocket infrastructure

# Define custom exceptions for realtime module
class AuthorizationError(Exception):
    """Raised when authorization fails"""
    pass

class NotConnectedError(Exception):
    """Raised when not connected to realtime service"""
    pass

class RealtimeError(Exception):
    """Base exception for realtime errors"""
    pass

class ConnectionError(RealtimeError):
    """Raised when connection fails"""
    pass

class SubscriptionError(RealtimeError):
    """Raised when subscription fails"""
    pass
