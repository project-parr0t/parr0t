#!/usr/bin/env python3
"""Synchronize localized Steam Workshop metadata through Valve's native Steam API."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = "parr0t.steam-workshop-metadata.v1"
SUBMIT_ITEM_UPDATE_CALLBACK = 3404
RESULT_OK = 1
VERIFICATION_LOCALES = ("english", "koreana")
REQUEST_GAP_SECONDS = 1.0
CALL_TIMEOUT_SECONDS = 30.0
INVALID_UPDATE_HANDLE = (1 << 64) - 1
INVALID_QUERY_HANDLE = (1 << 64) - 1
UGC_DETAILS_TAG_BYTES = 1025
MAX_TAGS = 100
MAX_TAG_BYTES = 255
UGC_DETAILS_TITLE_OFFSET = 24
UGC_DETAILS_TITLE_BYTES = 129
UGC_DETAILS_DESCRIPTION_OFFSET = UGC_DETAILS_TITLE_OFFSET + UGC_DETAILS_TITLE_BYTES
UGC_DETAILS_DESCRIPTION_BYTES = 8000
UGC_DETAILS_BUFFER_BYTES = 16384
SUPPORTED_LOCALES = frozenset(
    {
        "arabic",
        "bulgarian",
        "schinese",
        "tchinese",
        "czech",
        "danish",
        "dutch",
        "english",
        "finnish",
        "french",
        "german",
        "greek",
        "hungarian",
        "indonesian",
        "italian",
        "japanese",
        "koreana",
        "malay",
        "norwegian",
        "polish",
        "portuguese",
        "brazilian",
        "romanian",
        "russian",
        "spanish",
        "latam",
        "swedish",
        "thai",
        "turkish",
        "ukrainian",
        "vietnamese",
    }
)


class MetadataError(RuntimeError):
    """Raised when metadata or a Steam update is invalid."""


@dataclass(frozen=True)
class MetadataPlan:
    app_id: int
    item_id: int
    owner_id: int
    title: str
    description: str
    locales: tuple[str, ...]
    required_tags: tuple[str, ...] = ()


class SubmitItemUpdateResult(ctypes.Structure):
    _fields_ = [
        ("result", ctypes.c_int32),
        ("needs_legal_agreement", ctypes.c_bool),
        ("item_id", ctypes.c_uint64),
    ]


class SteamParamStringArray(ctypes.Structure):
    _fields_ = [
        ("strings", ctypes.POINTER(ctypes.c_char_p)),
        ("count", ctypes.c_int32),
    ]


class SteamUGCDetailsPrefix(ctypes.Structure):
    _fields_ = [
        ("item_id", ctypes.c_uint64),
        ("result", ctypes.c_int32),
        ("file_type", ctypes.c_int32),
        ("creator_app_id", ctypes.c_uint32),
        ("consumer_app_id", ctypes.c_uint32),
        ("title", ctypes.c_char * UGC_DETAILS_TITLE_BYTES),
        ("description", ctypes.c_char * UGC_DETAILS_DESCRIPTION_BYTES),
        ("owner_id", ctypes.c_uint64),
        ("created", ctypes.c_uint32),
        ("updated", ctypes.c_uint32),
        ("added_to_user_list", ctypes.c_uint32),
        ("visibility", ctypes.c_int32),
        ("banned", ctypes.c_bool),
        ("accepted_for_use", ctypes.c_bool),
        ("tags_truncated", ctypes.c_bool),
        ("tags", ctypes.c_char * UGC_DETAILS_TAG_BYTES),
    ]


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MetadataError(f"{field} must be a positive integer")
    return value


def _read_relative_text(manifest_path: Path, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise MetadataError("description_file must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise MetadataError("description_file must be relative to the manifest")
    root = manifest_path.parent.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MetadataError("description_file escapes the manifest directory") from exc
    description = resolved.read_text(encoding="utf-8")
    if not description.strip():
        raise MetadataError("description_file is empty")
    return description


def _required_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MetadataError("required_tags must be a list")
    if len(value) > MAX_TAGS:
        raise MetadataError(f"required_tags exceeds Steam's {MAX_TAGS}-tag limit")
    tags: list[str] = []
    keys: set[str] = set()
    for tag in value:
        if not isinstance(tag, str) or not tag or tag != tag.strip() or "\0" in tag:
            raise MetadataError("required_tags contains an invalid tag")
        if len(tag.encode("utf-8")) > MAX_TAG_BYTES:
            raise MetadataError(f"required_tags contains a tag over {MAX_TAG_BYTES} UTF-8 bytes")
        key = tag.casefold()
        if key in keys:
            raise MetadataError("required_tags contains duplicates")
        keys.add(key)
        tags.append(tag)
    return tuple(tags)


def merge_required_tags(current: Iterable[str], required: Iterable[str]) -> tuple[str, ...]:
    merged = list(current)
    keys = {tag.casefold() for tag in merged}
    for tag in required:
        key = tag.casefold()
        if key not in keys:
            merged.append(tag)
            keys.add(key)
    return tuple(merged)



def decode_details_tags(raw: bytes) -> tuple[str, ...]:
    tags_offset = SteamUGCDetailsPrefix.tags.offset
    tags_end = tags_offset + UGC_DETAILS_TAG_BYTES
    if len(raw) < tags_end:
        raise MetadataError("Steam returned an undersized Workshop detail record")
    if raw[SteamUGCDetailsPrefix.tags_truncated.offset]:
        raise MetadataError("Steam truncated the current Workshop tag list")
    value = raw[tags_offset:tags_end].split(b"\0", 1)[0].decode("utf-8", errors="strict")
    return tuple(tag.strip() for tag in value.split(",") if tag.strip())

def load_plan(manifest_path: Path) -> MetadataPlan:
    source = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict) or source.get("schema") != SCHEMA:
        raise MetadataError(f"manifest schema must be {SCHEMA}")
    title = source.get("title")
    if not isinstance(title, str) or not title.strip():
        raise MetadataError("title must be a non-empty string")
    if len(title.encode("utf-8")) > 128:
        raise MetadataError("title exceeds Steam's 128-byte limit")
    description = _read_relative_text(manifest_path, source.get("description_file"))
    if len(description.encode("utf-8")) > 8000:
        raise MetadataError("description exceeds Steam's 8000-byte limit")
    locales_value = source.get("locales")
    if not isinstance(locales_value, list) or not locales_value:
        raise MetadataError("locales must be a non-empty list")
    if any(not isinstance(locale, str) or locale not in SUPPORTED_LOCALES for locale in locales_value):
        raise MetadataError("locales contains an unsupported Steam language code")
    locales = tuple(locales_value)
    if len(set(locales)) != len(locales):
        raise MetadataError("locales contains duplicates")
    return MetadataPlan(
        app_id=_positive_int(source.get("app_id"), "app_id"),
        item_id=_positive_int(source.get("published_file_id"), "published_file_id"),
        owner_id=_positive_int(source.get("owner_steam_id"), "owner_steam_id"),
        title=title,
        description=description,
        locales=locales,
        required_tags=_required_tags(source.get("required_tags")),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SteamMetadataClient:
    def __init__(self, steam_api: Path, app_id: int, owner_id: int) -> None:
        if sys.platform != "win32":
            raise MetadataError("native Steam metadata synchronization currently requires Windows")
        if not steam_api.is_file():
            raise MetadataError(f"Steam API library does not exist: {steam_api}")
        os.environ["SteamAppId"] = str(app_id)
        os.environ["SteamGameId"] = str(app_id)
        self._dll = ctypes.WinDLL(str(steam_api.resolve()))
        self._configure()
        if self._init_flat is not None:
            error = ctypes.create_string_buffer(1024)
            result = self._init_flat(error)
            if result != 0:
                message = error.value.decode("utf-8", errors="replace")
                raise MetadataError(f"SteamAPI_InitFlat failed ({result}): {message}")
        elif not self._dll.SteamAPI_Init():
            raise MetadataError("SteamAPI_Init failed")
        self._active = True
        self._ugc = self._resolve_interface("SteamAPI_SteamUGC", range(21, 13, -1))
        self._utils = self._resolve_interface("SteamAPI_SteamUtils", range(10, 8, -1))
        self._user = self._resolve_interface("SteamAPI_SteamUser", range(23, 16, -1))
        if not self._ugc or not self._utils or not self._user:
            self.close()
            raise MetadataError("Steam UGC, utility, or user interface is unavailable")
        if not self._dll.SteamAPI_ISteamUser_BLoggedOn(self._user):
            self.close()
            raise MetadataError("Steam client is offline or unauthenticated")
        actual_owner = self._dll.SteamAPI_ISteamUser_GetSteamID(self._user)
        if actual_owner != owner_id:
            self.close()
            raise MetadataError(f"Steam client account {actual_owner} does not match manifest owner")
        self._next_request_at = 0.0

    def _resolve_interface(self, prefix: str, versions: Iterable[int]) -> int:
        for version in versions:
            try:
                function = getattr(self._dll, f"{prefix}_v{version:03d}")
            except AttributeError:
                continue
            function.restype = ctypes.c_void_p
            pointer = function()
            if pointer:
                return pointer
        return 0

    def _configure(self) -> None:
        dll = self._dll
        try:
            self._init_flat = dll.SteamAPI_InitFlat
        except AttributeError:
            self._init_flat = None
            dll.SteamAPI_Init.argtypes = []
            dll.SteamAPI_Init.restype = ctypes.c_bool
        else:
            self._init_flat.argtypes = [ctypes.c_void_p]
            self._init_flat.restype = ctypes.c_int32
        dll.SteamAPI_ISteamUGC_StartItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64]
        dll.SteamAPI_ISteamUGC_StartItemUpdate.restype = ctypes.c_uint64
        for name in (
            "SteamAPI_ISteamUGC_SetItemUpdateLanguage",
            "SteamAPI_ISteamUGC_SetItemTitle",
            "SteamAPI_ISteamUGC_SetItemDescription",
        ):
            function = getattr(dll, name)
            function.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p]
            function.restype = ctypes.c_bool
        dll.SteamAPI_ISteamUGC_SetItemTags.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(SteamParamStringArray),
        ]
        dll.SteamAPI_ISteamUGC_SetItemTags.restype = ctypes.c_bool
        dll.SteamAPI_ISteamUGC_SubmitItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p]
        dll.SteamAPI_ISteamUGC_SubmitItemUpdate.restype = ctypes.c_uint64
        dll.SteamAPI_ISteamUtils_IsAPICallCompleted.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_bool),
        ]
        dll.SteamAPI_ISteamUtils_IsAPICallCompleted.restype = ctypes.c_bool
        dll.SteamAPI_ISteamUtils_GetAPICallResult.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_bool),
        ]
        dll.SteamAPI_ISteamUtils_GetAPICallResult.restype = ctypes.c_bool
        dll.SteamAPI_ISteamUser_BLoggedOn.argtypes = [ctypes.c_void_p]
        dll.SteamAPI_ISteamUser_BLoggedOn.restype = ctypes.c_bool
        dll.SteamAPI_ISteamUser_GetSteamID.argtypes = [ctypes.c_void_p]
        dll.SteamAPI_ISteamUser_GetSteamID.restype = ctypes.c_uint64
        dll.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
        ]
        dll.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest.restype = ctypes.c_uint64
        dll.SteamAPI_ISteamUGC_SetLanguage.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_char_p,
        ]
        dll.SteamAPI_ISteamUGC_SetLanguage.restype = ctypes.c_bool
        dll.SteamAPI_ISteamUGC_SetReturnLongDescription.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_bool,
        ]
        dll.SteamAPI_ISteamUGC_SetReturnLongDescription.restype = ctypes.c_bool
        dll.SteamAPI_ISteamUGC_SendQueryUGCRequest.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        dll.SteamAPI_ISteamUGC_SendQueryUGCRequest.restype = ctypes.c_uint64
        dll.SteamAPI_ISteamUGC_GetQueryUGCResult.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        dll.SteamAPI_ISteamUGC_GetQueryUGCResult.restype = ctypes.c_bool
        dll.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        dll.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest.restype = ctypes.c_bool
        dll.SteamAPI_RunCallbacks.argtypes = []
        dll.SteamAPI_Shutdown.argtypes = []

    def close(self) -> None:
        if getattr(self, "_active", False):
            self._dll.SteamAPI_Shutdown()
            self._active = False

    def __enter__(self) -> "SteamMetadataClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def _utf8(value: str) -> bytes:
        return value.encode("utf-8")

    def _send(self, function: Callable[..., int], *args: Any) -> int:
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        if not self._dll.SteamAPI_ISteamUser_BLoggedOn(self._user):
            raise MetadataError("Steam client is offline or unauthenticated")
        return function(self._ugc, *args)

    def update(self, plan: MetadataPlan, locale: str) -> None:
        handle = self._dll.SteamAPI_ISteamUGC_StartItemUpdate(self._ugc, plan.app_id, plan.item_id)
        if handle == INVALID_UPDATE_HANDLE:
            raise MetadataError(f"Steam rejected the update handle for {locale}")
        setters = (
            ("language", self._dll.SteamAPI_ISteamUGC_SetItemUpdateLanguage, locale),
            ("title", self._dll.SteamAPI_ISteamUGC_SetItemTitle, plan.title),
            ("description", self._dll.SteamAPI_ISteamUGC_SetItemDescription, plan.description),
        )
        for field, setter, value in setters:
            if not setter(self._ugc, handle, self._utf8(value)):
                raise MetadataError(f"Steam rejected {field} for {locale}")
        call = self._send(
            self._dll.SteamAPI_ISteamUGC_SubmitItemUpdate,
            handle,
            self._utf8("Synchronize localized Workshop metadata"),
        )
        if not call:
            raise MetadataError(f"Steam rejected metadata submission for {locale}")
        result = self._wait(call)
        if result.result != RESULT_OK:
            raise MetadataError(f"Steam metadata update failed for {locale} with EResult {result.result}")
        if result.item_id != plan.item_id:
            raise MetadataError(f"Steam returned an unexpected Workshop item for {locale}")
        if result.needs_legal_agreement:
            raise MetadataError("Steam requires acceptance of the Workshop legal agreement")

    def update_tags(self, plan: MetadataPlan) -> bool:
        current = self._query_tags(plan)
        merged = merge_required_tags(current, plan.required_tags)
        if merged == current:
            return False
        handle = self._dll.SteamAPI_ISteamUGC_StartItemUpdate(self._ugc, plan.app_id, plan.item_id)
        if handle == INVALID_UPDATE_HANDLE:
            raise MetadataError("Steam rejected the tag update handle")
        encoded = [self._utf8(tag) for tag in merged]
        strings = (ctypes.c_char_p * len(encoded))(*encoded)
        values = SteamParamStringArray(
            ctypes.cast(strings, ctypes.POINTER(ctypes.c_char_p)),
            len(encoded),
        )
        if not self._dll.SteamAPI_ISteamUGC_SetItemTags(
            self._ugc, handle, ctypes.byref(values)
        ):
            raise MetadataError("Steam rejected Workshop tags")
        call = self._send(
            self._dll.SteamAPI_ISteamUGC_SubmitItemUpdate, handle, self._utf8("Add required Workshop tags")
        )
        if not call:
            raise MetadataError("Steam rejected tag submission")
        result = self._wait(call)
        if result.result != RESULT_OK:
            raise MetadataError(f"Steam tag update failed with EResult {result.result}")
        if result.item_id != plan.item_id:
            raise MetadataError("Steam returned an unexpected Workshop item for tag update")
        if result.needs_legal_agreement:
            raise MetadataError("Steam requires acceptance of the Workshop legal agreement")
        verified = self._query_tags(plan)
        missing = {tag.casefold() for tag in plan.required_tags}.difference(
            tag.casefold() for tag in verified
        )
        if missing:
            raise MetadataError("Steam tag verification mismatch")
        return True

    def _query_tags(self, plan: MetadataPlan) -> tuple[str, ...]:
        item_id = ctypes.c_uint64(plan.item_id)
        query = self._dll.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest(
            self._ugc, ctypes.byref(item_id), 1
        )
        if query == INVALID_QUERY_HANDLE:
            raise MetadataError("Steam rejected the tag verification query")
        try:
            call = self._send(self._dll.SteamAPI_ISteamUGC_SendQueryUGCRequest, query)
            if not call:
                raise MetadataError("Steam rejected the tag verification request")
            self._wait_for_completion(call)
            details = ctypes.create_string_buffer(UGC_DETAILS_BUFFER_BYTES)
            if not self._dll.SteamAPI_ISteamUGC_GetQueryUGCResult(
                self._ugc, query, 0, details
            ):
                raise MetadataError("Steam returned no tag verification result")
            raw = details.raw
            returned_item = int.from_bytes(raw[0:8], "little")
            result = int.from_bytes(raw[8:12], "little", signed=True)
            if result != RESULT_OK or returned_item != plan.item_id:
                raise MetadataError("Steam returned an invalid Workshop item for tag verification")
            return decode_details_tags(raw)
        finally:
            self._dll.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest(self._ugc, query)

    def _wait(self, call: int) -> SubmitItemUpdateResult:
        self._wait_for_completion(call)
        result = SubmitItemUpdateResult()
        call_failed = ctypes.c_bool(False)
        received = self._dll.SteamAPI_ISteamUtils_GetAPICallResult(
            self._utils,
            call,
            ctypes.byref(result),
            ctypes.sizeof(result),
            SUBMIT_ITEM_UPDATE_CALLBACK,
            ctypes.byref(call_failed),
        )
        if not received or call_failed.value:
            raise MetadataError("Steam did not return a valid metadata update result")
        return result

    def verify(self, plan: MetadataPlan, locale: str) -> None:
        item_id = ctypes.c_uint64(plan.item_id)
        query = self._dll.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest(
            self._ugc, ctypes.byref(item_id), 1
        )
        if query == INVALID_QUERY_HANDLE:
            raise MetadataError(f"Steam rejected metadata verification for {locale}")
        try:
            if not self._dll.SteamAPI_ISteamUGC_SetLanguage(
                self._ugc, query, self._utf8(locale)
            ):
                raise MetadataError(f"Steam rejected verification language {locale}")
            if not self._dll.SteamAPI_ISteamUGC_SetReturnLongDescription(
                self._ugc, query, True
            ):
                raise MetadataError(f"Steam rejected long-description verification for {locale}")
            call = self._send(self._dll.SteamAPI_ISteamUGC_SendQueryUGCRequest, query)
            if not call:
                raise MetadataError(f"Steam rejected metadata verification query for {locale}")
            self._wait_for_completion(call)
            details = ctypes.create_string_buffer(UGC_DETAILS_BUFFER_BYTES)
            if not self._dll.SteamAPI_ISteamUGC_GetQueryUGCResult(
                self._ugc, query, 0, details
            ):
                raise MetadataError(f"Steam returned no metadata verification result for {locale}")
            raw = details.raw
            returned_item = int.from_bytes(raw[0:8], "little")
            result = int.from_bytes(raw[8:12], "little", signed=True)
            title = raw[
                UGC_DETAILS_TITLE_OFFSET : UGC_DETAILS_TITLE_OFFSET + UGC_DETAILS_TITLE_BYTES
            ].split(b"\0", 1)[0].decode("utf-8", errors="strict")
            description = raw[
                UGC_DETAILS_DESCRIPTION_OFFSET : UGC_DETAILS_DESCRIPTION_OFFSET
                + UGC_DETAILS_DESCRIPTION_BYTES
            ].split(b"\0", 1)[0].decode("utf-8", errors="strict")
            if returned_item != plan.item_id:
                raise MetadataError("Steam returned an unexpected Workshop item for metadata verification")
            if result != RESULT_OK:
                raise MetadataError(f"Steam metadata verification failed for {locale} with EResult {result}")
            if title != plan.title or description != plan.description:
                raise MetadataError(f"Steam metadata verification mismatch for {locale}")
        finally:
            self._dll.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest(self._ugc, query)

    def _wait_for_completion(self, call: int) -> None:
        deadline = time.monotonic() + CALL_TIMEOUT_SECONDS
        while True:
            self._dll.SteamAPI_RunCallbacks()
            failed = ctypes.c_bool(False)
            if self._dll.SteamAPI_ISteamUtils_IsAPICallCompleted(
                self._utils, call, ctypes.byref(failed)
            ):
                if failed.value:
                    raise MetadataError("Steam API call failed before returning a result")
                self._next_request_at = time.monotonic() + REQUEST_GAP_SECONDS
                return
            if time.monotonic() >= deadline:
                raise MetadataError(f"Steam API call timed out after {CALL_TIMEOUT_SECONDS:g} seconds")
            time.sleep(0.05)


def describe_plan(plan: MetadataPlan, steam_api: Path) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "action": "steam_workshop_localized_metadata_update",
        "app_id": plan.app_id,
        "published_file_id": plan.item_id,
        "owner_steam_id": plan.owner_id,
        "title": plan.title,
        "description_sha256": hashlib.sha256(plan.description.encode("utf-8")).hexdigest(),
        "locales": list(plan.locales),
        "required_tags": list(plan.required_tags),
        "steam_api": str(steam_api.resolve()),
        "steam_api_sha256": _sha256(steam_api),
    }


def synchronize(
    plan: MetadataPlan,
    steam_api: Path,
    locales: Iterable[str],
    *,
    tags_only: bool = False,
) -> None:
    selected = tuple(locales)
    with SteamMetadataClient(steam_api, plan.app_id, plan.owner_id) as client:
        if plan.required_tags:
            changed = client.update_tags(plan)
            state = "updated and verified" if changed else "already present"
            print(f"tags: {state}", flush=True)
        if tags_only:
            return
        for index, locale in enumerate(selected, start=1):
            client.update(plan, locale)
            print(f"[{index}/{len(selected)}] {locale}: submitted", flush=True)
        for locale in VERIFICATION_LOCALES:
            if locale in selected:
                client.verify(plan, locale)
                print(f"{locale}: verified through the Steam client API", flush=True)


def require_item_approval(plan: MetadataPlan, approved_item: int | None) -> None:
    if approved_item != plan.item_id:
        raise MetadataError("--approve-item must exactly match published_file_id when --apply is used")

def require_steam_api_approval(steam_api: Path, approved_sha256: str | None) -> str:
    actual = _sha256(steam_api)
    if approved_sha256 is None or approved_sha256.lower() != actual:
        raise MetadataError(
            "--approve-steam-api-sha256 must exactly match the selected Steam API library when --apply is used"
        )
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--steam-api", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-item", type=int)
    parser.add_argument("--approve-steam-api-sha256")
    parser.add_argument("--locale", action="append", choices=sorted(SUPPORTED_LOCALES))
    parser.add_argument(
        "--tags-only",
        action="store_true",
        help="apply and verify required_tags without changing localized title or description",
    )
    args = parser.parse_args()
    try:
        plan = load_plan(args.manifest.resolve())
        steam_api = args.steam_api.resolve()
        print(json.dumps(describe_plan(plan, steam_api), ensure_ascii=False, indent=2))
        if not args.apply:
            print("Dry run only; no Steam metadata was changed.")
            return 0
        require_item_approval(plan, args.approve_item)
        require_steam_api_approval(steam_api, args.approve_steam_api_sha256)
        selected = tuple(args.locale) if args.locale else plan.locales
        unknown = set(selected).difference(plan.locales)
        if unknown:
            raise MetadataError("selected locale is not declared by the manifest")
        if args.tags_only and not plan.required_tags:
            raise MetadataError("--tags-only requires at least one manifest required_tags entry")
        synchronize(plan, steam_api, selected, tags_only=args.tags_only)
        return 0
    except (MetadataError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
