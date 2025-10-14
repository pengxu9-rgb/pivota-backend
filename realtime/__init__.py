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

# Additional sync realtime classes
class SyncRealtimeChannel:
    """Sync realtime channel for WebSocket connections"""
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

class SyncRealtimeClient:
    """Sync realtime client for WebSocket connections"""
    def __init__(self, url: str, key: str):
        self.url = url
        self.key = key
        self.channels = {}
    
    def channel(self, channel_name: str, options: dict = None):
        """Create a channel"""
        channel = SyncRealtimeChannel(channel_name, options)
        self.channels[channel_name] = channel
        return channel
    
    def connect(self):
        """Connect to realtime service"""
        return self
    
    def disconnect(self):
        """Disconnect from realtime service"""
        return self

# Additional common realtime classes that might be needed
class RealtimeSubscription:
    """Realtime subscription handler"""
    def __init__(self, channel, callback):
        self.channel = channel
        self.callback = callback
        self.active = False
    
    def start(self):
        """Start subscription"""
        self.active = True
        return self
    
    def stop(self):
        """Stop subscription"""
        self.active = False
        return self

class RealtimeMessage:
    """Realtime message handler"""
    def __init__(self, event, payload):
        self.event = event
        self.payload = payload
        self.timestamp = None
    
    def ack(self):
        """Acknowledge message"""
        return True

class RealtimePresence:
    """Realtime presence handler"""
    def __init__(self, channel):
        self.channel = channel
        self.state = {}
    
    def track(self, state):
        """Track presence state"""
        self.state.update(state)
        return self
    
    def untrack(self, keys):
        """Untrack presence state"""
        for key in keys:
            self.state.pop(key, None)
        return self
