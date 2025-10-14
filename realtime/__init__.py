# Real-time metrics and WebSocket infrastructure

# Define custom exceptions for realtime module
class AuthorizationError(Exception):
    """Raised when authorization fails"""
    pass

class NotConnectedError(Exception):
    """Raised when not connected to realtime service"""
    pass
