"""Unity 2021.3 managed-mod patch adapter."""

from .bundle import build_patch_bundle, verify_patch_bundle
from .discovery import UnityInstallation, discover_unity_installation
from .payload import compile_runtime_payload

__all__ = [
    "UnityInstallation",
    "build_patch_bundle",
    "compile_runtime_payload",
    "discover_unity_installation",
    "verify_patch_bundle",
]
