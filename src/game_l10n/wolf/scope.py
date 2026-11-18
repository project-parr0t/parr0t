"""Public roles supported by the adapter's normalized extraction contract.

This is not a WolfDawn-wide schema claim. Unknown source/text objects are a raw
census, not display evidence. Producers enumerate independently against these
same format rules; game-specific words and directory names are never roles.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


ROLE_SPECIFICATION = "wolf-normalized-display-roles-v1"
VISIBLE_SCRIPT_COMMANDS = (
    (r"^(@putselect\s+)(.*)$", "choice"),
    (r"^(@select\s+\S+\s+\S+\s+)(.*)$", "choice_prompt"),
    (r"^(@toki-teach\s+)(.*)$", "notification"),
    (r"^(@placename\s+)(.*)$", "place_name"),
    (r"^(@subtitle\s+\S+\s+)(.*)$", "subtitle"),
    (r"^(@jimaku\s+)(.*)$", "caption"),
    (r"^(@mono_txt\s+)(.*)$", "monologue"),
)


def structured_role(path: tuple[Any, ...]) -> str | None:
    # Existing event extraction exposes scene lines, but does not supply the
    # originating WScript opcode. Retain them as possible dependencies, not as
    # proven direct render operands. Other shapes require parser repair.
    if (len(path) == 4 and path[0] == "scenes" and isinstance(path[1], int)
            and path[2] == "lines" and isinstance(path[3], int)):
        return "event_line_dependency"
    return None


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def occurrence(ref: dict[str, Any], source: str, role: str,
               classification: str) -> dict[str, Any]:
    return {"ref": ref, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "role": role, "classification": classification,
            "role_specification": ROLE_SPECIFICATION}


def target_metadata(source: str, ref: dict[str, Any], role: str) -> dict[str, Any]:
    kind = ("possible_display_dependency" if role in {"event_line_dependency", "safe_name_dependency"}
            else "positive_display_binding")
    proof = occurrence(ref, source, role, kind)
    return {"display_binding_id": "wolf:" + fingerprint(ref),
            "display_scope_evidence": {"kind": kind, "source_sha256": proof["source_sha256"],
                                       "evidence_fingerprint": fingerprint(proof),
                                       "binding": proof}}


def scope_coverage(records: list[dict[str, Any]], candidate_count: int,
                   excluded_json_names: list[str]) -> dict[str, Any]:
    unclassified = sum(row["classification"] == "unclassified" for row in records)
    exclusions = [{"reason_code": "WOLF_OPERATOR_EXCLUDED_JSON", "affects_user_visible_text": True,
                   "scope": name} for name in sorted(set(excluded_json_names))]
    if unclassified:
        exclusions.append({"reason_code": "WOLF_UNCLASSIFIED_FORMAT_ROLE",
                           "affects_user_visible_text": True, "occurrence_count": unclassified})
    return {"declared_complete": not exclusions, "exclusions": exclusions,
            "display_scope": {"schema": "parr0t.display-scope.v1",
                              "raw_occurrence_count": len(records),
                              "display_candidate_count": candidate_count,
                              "control_only_count": sum(row["classification"] == "control_only" for row in records),
                              "unclassified_count": unclassified,
                              "evidence_fingerprint": fingerprint(records)}}
