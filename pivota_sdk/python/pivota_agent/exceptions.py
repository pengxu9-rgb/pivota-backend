"""
Pivota Agent SDK Exceptions
"""

class PivotaAPIError(Exception):
    """Base exception for all Pivota API errors"""
    def __init__(self, message, status_code=None, response=None):
        self.message = message
        self.status_code = status_code
        self.response = response
        super().__init__(self.message)

class AuthenticationError(PivotaAPIError):
    """Raised when API key is invalid or missing"""
    pass

class RateLimitError(PivotaAPIError):
    """Raised when rate limit is exceeded"""
    def __init__(self, message, retry_after=None, **kwargs):
        self.retry_after = retry_after
        super().__init__(message, **kwargs)

class NotFoundError(PivotaAPIError):
    """Raised when resource is not found"""
    pass

class ValidationError(PivotaAPIError):
    """Raised when request validation fails"""
    pass




