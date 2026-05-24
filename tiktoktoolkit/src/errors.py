"""Custom exceptions for the toolkit."""


class UTTKError(Exception):
    """Base exception for all toolkit errors."""
    pass


class ProviderError(UTTKError):
    """Gallery-dl provider error."""
    pass


class ConfigError(UTTKError):
    """Configuration-related error."""
    pass


class DownloadError(UTTKError):
    """Download operation error."""
    pass


class ValidationError(UTTKError):
    """Input validation error."""
    pass


class AntiBotError(ProviderError):
    """TikTok anti-bot protection detected."""
    pass


class TrackerError(UTTKError):
    """Download tracker database error."""
    pass


class AuthenticationError(UTTKError):
    """Authentication or cookie-related error."""
    pass


class BrowserAutomationError(UTTKError):
    """Browser automation (Playwright) error."""
    pass
