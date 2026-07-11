class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing."""


class UpstreamServiceError(RuntimeError):
    """Raised when Prowlarr or qBittorrent cannot complete a request."""


class SharedDownloadControlError(RuntimeError):
    """Raised when requester control would affect other subscribers."""
