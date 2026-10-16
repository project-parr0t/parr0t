from __future__ import annotations


class GameL10nError(Exception):
    """Base SDK error."""


class ProviderError(GameL10nError):
    """A model provider failed or returned an invalid protocol response."""


class ProviderFailure(ProviderError):
    """A provider failure reduced to control-plane-safe categorical metadata."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class ConfigurationError(GameL10nError):
    """Project configuration is invalid."""


class ToolExecutionError(GameL10nError):
    """An unpack/pack tool failed; its message is private data-plane diagnostics."""


class RuntimeApprovalError(GameL10nError):
    """A real installation, removal, update, or launch lacks exact user approval."""
