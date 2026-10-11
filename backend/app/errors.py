class AppError(Exception):
    def __init__(self, code: str, message: str, status=400, details=None, retryable=False):
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        self.retryable = retryable
        super().__init__(message)
