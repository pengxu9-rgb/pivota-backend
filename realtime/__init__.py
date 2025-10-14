# Real-time metrics and WebSocket infrastructure

# Define custom exceptions for realtime module
class AuthorizationError(Exception):
    """Raised when authorization fails"""
    pass

class NotConnectedError(Exception):
    """Raised when not connected to realtime service"""
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
    def __init__(self, url: str, key: str):
        self.url = url
        self.key = key
        self.channels = {}
    
    def channel(self, channel_name: str, options: dict = None):
        """Create a channel"""
        channel = AsyncRealtimeChannel(channel_name, options)
        self.channels[channel_name] = channel
        return channel
    
    async def connect(self):
        """Connect to realtime service"""
        return self
    
    async def disconnect(self):
        """Disconnect from realtime service"""
        return self

class RealtimeChannelOptions:
    """Options for realtime channel configuration"""
    def __init__(self, **kwargs):
        self.config = kwargs

# Additional common realtime classes
class RealtimeChannel:
    """Realtime channel for WebSocket connections"""
    def __init__(self, channel_name: str, options: dict = None):
        self.channel_name = channel_name
        self.options = options or {}
        self.connected = False
    
    def subscribe(self, callback):
        """Subscribe to channel events"""
        self.connected = True
        return self
    
    def unsubscribe(self):
        """Unsubscribe from channel"""
        self.connected = False
        return self

class RealtimeClient:
    """Realtime client for WebSocket connections"""
    def __init__(self, url: str, key: str):
        self.url = url
        self.key = key
        self.channels = {}
    
    def channel(self, channel_name: str, options: dict = None):
        """Create a channel"""
        channel = RealtimeChannel(channel_name, options)
        self.channels[channel_name] = channel
        return channel
    
    def connect(self):
        """Connect to realtime service"""
        return self
    
    def disconnect(self):
        """Disconnect from realtime service"""
        return self

# Additional exceptions
class RealtimeError(Exception):
    """Base exception for realtime errors"""
    pass

class ConnectionError(RealtimeError):
    """Raised when connection fails"""
    pass

class SubscriptionError(RealtimeError):
    """Raised when subscription fails"""
    pass
