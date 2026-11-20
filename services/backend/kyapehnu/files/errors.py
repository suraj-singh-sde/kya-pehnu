"""What the picture store can say no with. The HTTP mapping is the global handler's (core/errors.py)."""

from kyapehnu.core.errors import NotFoundError, ServiceUnavailableError


class MissingFileError(NotFoundError):
    """No picture at that key. The same text as another user's picture and a bad name, so a 404 reveals nothing."""

    message = "Image not found"


class StoreUnavailableError(ServiceUnavailableError):
    """The store cannot answer (network, credentials, permissions, a gone bucket). Retryable; says nothing more."""

    code = "files_unavailable"
    message = "Pictures are unavailable right now. Retry shortly."


class InvalidKeyError(ValueError):
    """A key that is not a picture address: a programming error, never user input (routes validate first)."""
