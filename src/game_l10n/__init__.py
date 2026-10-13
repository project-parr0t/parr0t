"""Parr0t refusal-aware game localization orchestration framework."""

from .models import (
    CharacterProfile,
    ContextMode,
    ContextPack,
    GlossaryEntry,
    ProjectContext,
    Segment,
    TranslationResult,
)
from .orchestrator import LocalizationOrchestrator, OrchestratorConfig
from .providers import ModelProvider, ProviderRegistry
from .steam import build_steam_package, validate_steam_package

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
    "build_steam_package",
    "validate_steam_package",
]

__version__ = "0.2.0"
