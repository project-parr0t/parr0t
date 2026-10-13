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
from .corrections import apply_user_corrections
from .orchestrator import LocalizationOrchestrator, OrchestratorConfig
from .providers import ModelProvider, ProviderRegistry
from .rpg_maker_mv import (
    RpgMakerMvError,
    apply_translations as apply_rpg_maker_mv_translations,
    extract_game as extract_rpg_maker_mv_game,
    pack_patch as pack_rpg_maker_mv_patch,
    verify_patch as verify_rpg_maker_mv_patch,
)
from .steam import build_steam_package, inspect_installed_state, validate_steam_package

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
    "RpgMakerMvError",
    "Segment",
    "TranslationResult",
    "apply_user_corrections",
    "apply_rpg_maker_mv_translations",
    "build_steam_package",
    "extract_rpg_maker_mv_game",
    "inspect_installed_state",
    "pack_rpg_maker_mv_patch",
    "validate_steam_package",
    "verify_rpg_maker_mv_patch",
]

__version__ = "0.1.0"
