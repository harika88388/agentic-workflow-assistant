class GoogleAPIError(Exception):
    def __init__(self, message: str, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.message, self.status, self.reason = message, status, reason


class GoogleAuthError(GoogleAPIError): """401 / insufficient permission: user must reconnect."""
class GoogleServiceDisabledError(GoogleAPIError): """403 API not enabled in the Google Cloud project."""
class GoogleNotFoundError(GoogleAPIError): """404 / 410."""
class GoogleConflictError(GoogleAPIError): """409, e.g. duplicate client-supplied event id."""
class GoogleRateLimitError(GoogleAPIError): """429 or quota 403."""
class GoogleTransientError(GoogleAPIError): """5xx or network failure."""
class GoogleBadRequestError(GoogleAPIError): """Other 4xx."""
