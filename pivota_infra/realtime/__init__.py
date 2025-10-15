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

# Define custom realtime classes
class AsyncRealtimeChannel:
    """Async realtime channel for WebSocket connections"""
    def __init__(self, channel_name: str, options: dict = None):
        self.channel_name = channel_name
        self.options = options or {}
        self.connected = False
    
    async def subscribe(self, callback):
        """Subscribe to channel events"""
        self.connected = True
        return self
    
    async def unsubscribe(self):
        """Unsubscribe from channel"""
        self.connected = False
        return self

class AsyncRealtimeClient:
    """Async realtime client for WebSocket connections"""
    def __init__(self, url: str, token: str = None, **options):
        self.url = url
        self.token = token
        self.key = token  # For backward compatibility
        self.options = options
        self.channels = {}
        self.connected = False
    
    def channel(self, channel_name: str, options: dict = None):
        """Create a channel"""
        channel = AsyncRealtimeChannel(channel_name, options)
        self.channels[channel_name] = channel
        return channel
    
    async def connect(self):
        """Connect to realtime service"""
        self.connected = True
        return self
    
    async def disconnect(self):
        """Disconnect from realtime service"""
        self.connected = False
        return self
    
    def set_auth(self, token: str):
        """Set authentication token"""
        self.token = token
        self.key = token
        return self
