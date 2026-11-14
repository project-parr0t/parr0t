from __future__ import annotations

import re
from typing import Any


GLOSSARY_AUTHORITIES = (
    "literal_fallback",
    "domain_standard",
    "source_internal",
    "project_glossary",
    "official_localization",
    "user_override",
)
GLOSSARY_AUTHORITY_RANK = {
    name: rank for rank, name in enumerate(GLOSSARY_AUTHORITIES)
}
SOURCE_LANGUAGE_AUTHORITY_SCHEMA = "parr0t.source-language-authority/v1"
SOURCE_LANGUAGE_REASON_CODES = {
    "original_implementation",
    "complete_source_catalog",
    "authoritative_source_metadata",
}
PARENTHETICAL_POLICY = "source_or_authoritative_glossary"
LOCALIZATION_AUTHORITY_FIELDS = (
    "source_language_authority",
    "terminology_concept_ids",
    "source_absent_parenthetical_policy",
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def source_language_authority(value: Any) -> dict[str, str]:
    """Validate and normalize evidence that overrides the project source language."""

    required = {"schema", "language", "reason_code", "evidence_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("source_language_authority has an invalid shape")
    if value.get("schema") != SOURCE_LANGUAGE_AUTHORITY_SCHEMA:
        raise ValueError("source_language_authority has an unsupported schema")
    language = value.get("language")
    reason_code = value.get("reason_code")
    evidence_sha256 = value.get("evidence_sha256")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("source_language_authority language must be nonempty")
    if reason_code not in SOURCE_LANGUAGE_REASON_CODES:
        raise ValueError("source_language_authority reason_code is unsupported")
    if not isinstance(evidence_sha256, str) or not _SHA256.fullmatch(evidence_sha256):
        raise ValueError("source_language_authority evidence_sha256 must be lowercase SHA-256")
    return {
        "schema": SOURCE_LANGUAGE_AUTHORITY_SCHEMA,
        "language": language.strip(),
        "reason_code": reason_code,
        "evidence_sha256": evidence_sha256,
    }


def effective_source_language(
    metadata: dict[str, Any], default_language: str
) -> dict[str, str]:
    """Return the source language the semantic pipeline must treat as authoritative."""

    value = metadata.get("source_language_authority")
    if value is None:
        return {
            "schema": "parr0t.project-source-language/v1",
            "language": default_language,
            "reason_code": "project_default",
        }
    return source_language_authority(value)


def localization_authority_contract(metadata: dict[str, Any]) -> dict[str, Any]:
    """Project immutable localization authority fields from inventory metadata."""

    contract: dict[str, Any] = {}
    if "source_language_authority" in metadata:
        contract["source_language_authority"] = source_language_authority(
            metadata["source_language_authority"]
        )
    if "terminology_concept_ids" in metadata:
        concept_ids = metadata["terminology_concept_ids"]
        if (
            not isinstance(concept_ids, list)
            or not all(isinstance(item, str) and item.strip() for item in concept_ids)
            or len({item.strip() for item in concept_ids}) != len(concept_ids)
        ):
            raise ValueError("terminology_concept_ids must be unique nonempty strings")
        contract["terminology_concept_ids"] = [item.strip() for item in concept_ids]
    if "source_absent_parenthetical_policy" in metadata:
        policy = metadata["source_absent_parenthetical_policy"]
        if policy != PARENTHETICAL_POLICY:
            raise ValueError("source_absent_parenthetical_policy is unsupported")
        contract["source_absent_parenthetical_policy"] = policy
    return contract
