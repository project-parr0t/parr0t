"""Unity 2021.3 Mono and IL2CPP patch adapter."""

from .bundle import build_patch_bundle, verify_patch_bundle
from .discovery import UnityInstallation, discover_unity_installation, read_il2cpp_metadata_version
from .il2cpp import build_il2cpp_bridge, generate_il2cpp_interop, inspect_bepinex_il2cpp
from .payload import compile_runtime_payload

__all__ = [
    "UnityInstallation",
    "build_patch_bundle",
    "build_il2cpp_bridge",
    "compile_runtime_payload",
    "discover_unity_installation",
    "generate_il2cpp_interop",
    "inspect_bepinex_il2cpp",
    "read_il2cpp_metadata_version",
    "verify_patch_bundle",
]
