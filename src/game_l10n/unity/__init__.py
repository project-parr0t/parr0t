"""Bound offline Unity 2021.1 Mono serialization support."""
from importlib.metadata import version

def require_dependencies():
    for name, expected in (("UnityPy", "1.23.0"), ("TypeTreeGeneratorAPI", "0.0.10")):
        if version(name) != expected:
            raise ValueError("unity_dependency_version_mismatch")

__all__ = ["require_dependencies"]
