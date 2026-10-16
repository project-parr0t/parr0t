"""Parr0t fallback-routed game localization orchestration framework."""

from .models import (
    CharacterProfile,
    ContextMode,
    ContextPack,
    GlossaryEntry,
    ProjectContext,
    Segment,
    TranslationResult,
)
from .corrections import apply_user_corrections
from .orchestrator import LocalizationOrchestrator, OrchestratorConfig
from .providers import ModelProvider, ProviderRegistry
from .steam import build_steam_package, inspect_installed_state, validate_steam_package
from .filesystem_package import build_filesystem_package, validate_filesystem_package
from .translation_exchange import prepare_translation_exchange, seal_translation_exchange

__all__ = [
    "CharacterProfile",
    "ContextMode",
    "ContextPack",
    "GlossaryEntry",
    "LocalizationOrchestrator",
    "ModelProvider",
    "OrchestratorConfig",
    "ProjectContext",
    "ProviderRegistry",
    "Segment",
    "TranslationResult",
    "apply_user_corrections",
    "build_steam_package",
    "build_filesystem_package",
    "inspect_installed_state",
    "validate_steam_package",
    "validate_filesystem_package",
    "prepare_translation_exchange",
    "seal_translation_exchange",
]

__version__ = "0.2.0"
