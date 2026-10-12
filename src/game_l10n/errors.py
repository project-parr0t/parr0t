class GameL10nError(Exception):
    """Base SDK error."""


class ProviderError(GameL10nError):
    """A model provider failed or returned an invalid protocol response."""


class ConfigurationError(GameL10nError):
    """Project configuration is invalid."""


class ToolExecutionError(GameL10nError):
    """An unpack/pack tool failed."""
