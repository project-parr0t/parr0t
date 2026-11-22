"""Select a single MZ text source, then extract and patch only its eligible slots.

Proofs and typed engine observations come from the SDK integration, not from
filenames or user text. This module verifies their source/slot bindings; it
neither discovers a loader nor infers a JavaScript receiver's runtime type.
All paths are caller-owned logical source IDs. No files are written here.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from itertools import pairwise
import json
from pathlib import PurePosixPath
import re
from typing import Any


class TextInventoryError(ValueError):
    """The supplied evidence or translation cannot be applied as bound."""


JsonPath = tuple[str | int, ...]
Span = tuple[int, int]


@dataclass(frozen=True, slots=True)
class SourceBinding:
    file: str
    sha256: str  # Exact input bytes, including any BOM.


@dataclass(frozen=True, slots=True)
class SpanBinding:
    start: int  # UTF-8 decoded code point offsets, excluding any BOM.
    end: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NewLocaleCatalog:
    """Loader-supported output destination using the source catalog as template."""

    file: str


@dataclass(frozen=True, slots=True)
class LocaleCatalogBinding:
    """Proven native JSON source and destination, with exact value addresses.

    Addresses can include array indices, but never wildcard keys. Source and
    target may use different addresses or share the same file.
    """

    source: SourceBinding
    target: SourceBinding | NewLocaleCatalog
    values: tuple[tuple[JsonPath, JsonPath], ...]


@dataclass(frozen=True, slots=True)
class LocaleCatalogProof:
    """SDK-attested loader span that resolves these catalogs for source and target."""

    loader: SourceBinding
    loader_evidence: SpanBinding
    catalogs: tuple[LocaleCatalogBinding, ...]
    declared_complete: bool = False


@dataclass(frozen=True, slots=True)
class LocaleUnavailable:
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class LocaleInconclusive:
    pass


class DataKind(Enum):
    MAP = "map"
    COMMON_EVENTS = "common_events"
    TROOPS = "troops"


@dataclass(frozen=True, slots=True)
class NativeDataObservation:
    """SDK observation that this exact resource is loaded in this MZ data role."""

    source: SourceBinding
    kind: DataKind


class ReceiverKind(Enum):
    WINDOW_BASE = "Window_Base"
    BITMAP = "Bitmap"
    GAME_MESSAGE = "Game_Message"


@dataclass(frozen=True, slots=True)
class ReceiverWitness:
    """SDK-attested native runtime receiver at this exact source expression."""

    kind: ReceiverKind
    expression: SpanBinding


@dataclass(frozen=True, slots=True)
class NativeRenderObservation:
    source: SourceBinding
    receiver: ReceiverWitness
    call: SpanBinding
    argument: SpanBinding


class ScreenReview(Enum):
    STATIC = "static"
    USER_SKIPPED = "user_skipped"


@dataclass(frozen=True, slots=True)
class DisplayObservations:
    """Positive observations and counts from the SDK's selected display scope.

    Unclassified raw occurrences here exclude the separately counted pending
    display dependencies; only those dependencies block a complete declaration.
    """

    data: tuple[NativeDataObservation, ...] = ()
    scripts: tuple[NativeRenderObservation, ...] = ()
    screen_review: ScreenReview = ScreenReview.STATIC
    pending_display_count: int = 0
    declared_complete: bool = False
    control_only_count: int = 0
    unclassified_count: int = 0


_SHA256 = re.compile(r"[0-9a-f]{64}")
_BOM = b"\xef\xbb\xbf"
_PROTECTED_MZ_TEXT = re.compile(r"\\(?:[A-Za-z]+\[[^\]\r\n]*\]|[.!|^{}$<>\\]|[A-Za-z]+)|%[1-9][0-9]*")


def _digest(text: str) -> str:
    try:
        return sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise TextInventoryError("Eligible text must be valid UTF-8") from exc


def _fingerprint(value: Any) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True, slots=True)
class _SourceFile:
    text: str
    sha256: str
    bom: bytes

    def verify_span(self, binding: SpanBinding) -> Span:
        start, end = binding.start, binding.end
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(self.text)
                or _digest(self.text[start:end]) != binding.sha256):
            raise TextInventoryError("Source span does not match its evidence")
        return start, end


class _Snapshot:
    def __init__(self, read_source: Callable[[str], bytes]) -> None:
        self.read_source = read_source
        self.files: dict[str, _SourceFile] = {}
        self.new_files: set[str] = set()
        self.json: dict[str, _JsonDocument] = {}
        self.scripts: dict[str, dict[Span, _Call]] = {}

    def file(self, binding: SourceBinding) -> _SourceFile:
        if not isinstance(binding.file, str) or not binding.file:
            raise TextInventoryError("Source binding needs a file identity")
        file = self.files.get(binding.file)
        if file is None:
            try:
                raw = self.read_source(binding.file)
                if not isinstance(raw, bytes) or sha256(raw).hexdigest() != binding.sha256:
                    raise TextInventoryError("Source file does not match its evidence")
                file = _SourceFile(raw.decode("utf-8-sig"), binding.sha256, _BOM if raw.startswith(_BOM) else b"")
            except (OSError, LookupError, UnicodeError) as exc:
                raise TextInventoryError("Bound source file is not readable UTF-8") from exc
            self.files[binding.file] = file
        elif file.sha256 != binding.sha256:
            raise TextInventoryError("Conflicting source file evidence")
        return file

    def create_catalog(self, file: str, template: SourceBinding) -> SourceBinding:
        path = PurePosixPath(file)
        if (not path.parts or path.is_absolute() or path.as_posix() != file or ".." in path.parts
                or any(char in file for char in "\\:\0") or file in self.files):
            raise TextInventoryError("Invalid or conflicting new catalog destination")
        self.files[file] = self.file(template)
        self.new_files.add(file)
        return SourceBinding(file, template.sha256)

    def json_document(self, binding: SourceBinding) -> _JsonDocument:
        source = self.file(binding)
        document = self.json.get(binding.file)
        if document is None:
            document = self.json[binding.file] = _JsonDocument(source.text)
        return document

    def script_calls(self, binding: SourceBinding) -> dict[Span, _Call]:
        source = self.file(binding)
        calls = self.scripts.get(binding.file)
        if calls is None:
            calls = self.scripts[binding.file] = _script_calls(source.text)
        return calls


_JSON_WHITESPACE = re.compile(r"[ \t\r\n]*")


def _invalid_constant(_: str) -> Any:
    raise TextInventoryError("Native data contains a non-JSON number")


_JSON_DECODER = json.JSONDecoder(parse_constant=_invalid_constant)


class _JsonDocument:
    """Decode native JSON while retaining only string-value patch addresses."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.offset = 0
        self.strings: dict[JsonPath, tuple[str, Span]] = {}
        try:
            self.root = self._value(())
            if self._peek():
                raise TextInventoryError("Unexpected suffix in native JSON data")
        except (json.JSONDecodeError, RecursionError) as exc:
            raise TextInventoryError("Invalid native JSON data") from exc

    def _peek(self) -> str:
        self.offset = _JSON_WHITESPACE.match(self.text, self.offset).end()
        return self.text[self.offset:self.offset + 1]

    def _consume(self, token: str) -> bool:
        if self._peek() != token:
            return False
        self.offset += 1
        return True

    def _expect(self, token: str) -> None:
        if not self._consume(token):
            raise TextInventoryError("Invalid native JSON delimiter")

    def _value(self, path: JsonPath) -> Any:
        token = self._peek()
        if token == "{":
            self.offset += 1
            obj: dict[str, Any] = {}
            if not self._consume("}"):
                while True:
                    if self._peek() != '"':
                        raise TextInventoryError("Invalid native JSON property")
                    key, self.offset = _JSON_DECODER.raw_decode(self.text, self.offset)
                    if key in obj:
                        raise TextInventoryError("Ambiguous duplicate native JSON property")
                    self._expect(":")
                    obj[key] = self._value((*path, key))
                    if self._consume("}"):
                        break
                    self._expect(",")
            return obj
        if token == "[":
            self.offset += 1
            array: list[Any] = []
            if not self._consume("]"):
                while True:
                    array.append(self._value((*path, len(array))))
                    if self._consume("]"):
                        break
                    self._expect(",")
            return array
        start = self.offset
        value, self.offset = _JSON_DECODER.raw_decode(self.text, start)
        if isinstance(value, str):
            self.strings[path] = value, (start, self.offset)
        return value

    def string(self, path: JsonPath) -> tuple[str, Span]:
        if not isinstance(path, tuple) or any(type(part) not in (str, int) for part in path):
            raise TextInventoryError("Invalid native JSON value address")
        try:
            return self.strings[path]
        except KeyError as exc:
            raise TextInventoryError("Bound address is not a native JSON string value") from exc


def _shape(condition: bool) -> None:
    if not condition:
        raise TextInventoryError("Data does not match its observed native MZ format")


def _indexed_records(data: Any) -> Iterator[tuple[int, dict[str, Any]]]:
    _shape(isinstance(data, list) and bool(data) and data[0] is None)
    for index in range(1, len(data)):
        record = data[index]
        if record is None:
            continue
        _shape(isinstance(record, dict) and type(record.get("id")) is int and record["id"] == index)
        yield index, record


def _command_slots(commands: Any, path: JsonPath) -> Iterator[JsonPath]:
    _shape(isinstance(commands, list))
    continuation: tuple[int, int] | None = None
    for index, command in enumerate(commands):
        _shape(isinstance(command, dict))
        code, indent, args = command.get("code"), command.get("indent"), command.get("parameters")
        _shape(type(code) is int and type(indent) is int and indent >= 0 and isinstance(args, list))
        params = (*path, index, "parameters")
        if continuation == (code, indent):
            _shape(len(args) == 1 and isinstance(args[0], str))
            yield (*params, 0)
            continue
        continuation = None
        if code == 101:  # Show Text: face ID and display settings aren't text.
            _shape(len(args) == 5 and isinstance(args[0], str) and isinstance(args[4], str)
                   and all(type(arg) is int for arg in args[1:4]))
            yield (*params, 4)
            continuation = 401, indent
        elif code == 105:  # Show Scrolling Text.
            _shape(len(args) == 2 and type(args[0]) is int and type(args[1]) is bool)
            continuation = 405, indent
        elif code == 102:  # Show Choices: branch labels (402) are not read as display text.
            _shape(len(args) == 5 and isinstance(args[0], list)
                   and all(isinstance(arg, str) for arg in args[0])
                   and all(type(arg) is int for arg in args[1:]))
            for choice in range(len(args[0])):
                yield (*params, 0, choice)


def _native_slots(root: Any, kind: DataKind) -> Iterator[JsonPath]:
    if kind is DataKind.MAP:
        _shape(isinstance(root, dict) and isinstance(root.get("displayName"), str))
        yield ("displayName",)  # Window_MapName reads Game_Map.displayName().
        records, prefix = root.get("events"), ("events",)
    elif kind in (DataKind.COMMON_EVENTS, DataKind.TROOPS):
        records, prefix = root, ()
    else:
        raise TextInventoryError("Unsupported native MZ data role")
    for index, record in _indexed_records(records):
        path = (*prefix, index)
        if kind is DataKind.COMMON_EVENTS:
            _shape(type(record.get("trigger")) is int and type(record.get("switchId")) is int)
            yield from _command_slots(record.get("list"), (*path, "list"))
            continue
        if kind is DataKind.MAP:
            _shape(type(record.get("x")) is int and type(record.get("y")) is int)
        else:
            _shape(isinstance(record.get("members"), list))
        pages = record.get("pages")
        _shape(isinstance(pages, list))
        for page_index, page in enumerate(pages):
            _shape(isinstance(page, dict) and isinstance(page.get("conditions"), dict))
            yield from _command_slots(page.get("list"), (*path, "pages", page_index, "list"))


# Closed JavaScript subset: whole units of semicolon-terminated direct member
# calls, with scalar literals or simple member references as arguments. No
# execution, AST guessing through unparsed context, templates, regexps, nested
# expressions, dynamic properties, or implicit semicolon insertion.
_NAME = r"[A-Za-z_$][A-Za-z0-9_$]*"
_JS_NUMBER = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
_JS_ESCAPE = r'''\\(?:["'\\/bfnrtv]|0(?![0-9])|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4})'''
_JS_TOKEN = re.compile(
    r"(?P<trivia>[ \t\r\n\u2028\u2029\ufeff]+|//[^\r\n\u2028\u2029]*|/\*[\s\S]*?\*/)"
    rf'''|(?P<string>"(?:[^"\\\x00-\x1f\u2028\u2029]|{_JS_ESCAPE})*"|'''
    rf"'(?:[^'\\\x00-\x1f\u2028\u2029]|{_JS_ESCAPE})*')"
    rf"|(?P<number>{_JS_NUMBER})|(?P<name>{_NAME})|(?P<punct>[.,();])"
)
_JS_RESERVED = frozenset("""
    await break case catch class const continue debugger default delete do else enum export extends
    false finally for function if implements import in instanceof interface let new null package
    private protected public return static super switch throw true try typeof var void while with yield
""".split())
_RENDER_ARITY = {
    (ReceiverKind.WINDOW_BASE, "drawText"): (4, 5),
    (ReceiverKind.WINDOW_BASE, "drawTextEx"): (4,),
    (ReceiverKind.BITMAP, "drawText"): (5, 6),
    (ReceiverKind.GAME_MESSAGE, "add"): (1,),
}


@dataclass(frozen=True, slots=True)
class _Call:
    receiver: Span
    method: str
    first_string: Span | None
    arity: int


def _unsupported_script() -> TextInventoryError:
    return TextInventoryError("Script is outside the supported direct MZ render syntax")


def _script_calls(text: str) -> dict[Span, _Call]:
    def tokenize() -> Iterator[re.Match[str]]:
        offset = 0
        while offset < len(text):
            token = _JS_TOKEN.match(text, offset)
            if token is None:
                raise _unsupported_script()
            offset = token.end()
            if token.lastgroup != "trivia":
                yield token

    tokens = iter(tokenize())
    token = next(tokens, None)

    def consume(expected: str | None = None, kind: str | None = None) -> re.Match[str]:
        nonlocal token
        if (token is None or (expected is not None and token[0] != expected)
                or (kind is not None and token.lastgroup != kind)):
            raise _unsupported_script()
        previous = token
        token = next(tokens, None)
        return previous

    def reference() -> tuple[re.Match[str], re.Match[str], re.Match[str] | None]:
        root = last = consume(kind="name")
        if root[0] in _JS_RESERVED:
            raise _unsupported_script()
        previous = None
        while token is not None and token[0] == ".":
            consume(".")
            previous, last = last, consume(kind="name")
        return root, last, previous

    calls = {}
    while token is not None:
        root, method, receiver_end = reference()
        if receiver_end is None:
            raise _unsupported_script()
        consume("(")
        first_string = None
        arity = 0
        if token is not None and token[0] != ")":
            while True:
                if token is None:
                    raise _unsupported_script()
                if token.lastgroup == "string":
                    span = consume(kind="string").span()
                    if arity == 0:
                        first_string = span
                elif token.lastgroup == "number" or token[0] in ("true", "false", "null"):
                    consume()
                else:
                    reference()
                arity += 1
                if token is None or token[0] != ",":
                    break
                consume(",")
        close = consume(")")
        consume(";")
        calls[root.start(), close.end()] = _Call((root.start(), receiver_end.end()), method[0], first_string, arity)
    return calls


_JS_ESCAPE_RE = re.compile(_JS_ESCAPE)
_JS_CONTROL = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}


def _decode_js_string(literal: str) -> str:
    def unescape(match: re.Match[str]) -> str:
        code = match[0][1:]
        if code[0] in "xu":
            return chr(int(code[1:], 16))
        return _JS_CONTROL.get(code, code)

    text = _JS_ESCAPE_RE.sub(unescape, literal[1:-1])
    # JavaScript \u escapes describe UTF-16 code units, not Unicode points.
    try:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError as exc:
        raise TextInventoryError("Eligible script text has an unpaired surrogate") from exc


@dataclass(frozen=True, slots=True)
class _Target:
    id: str
    value: str
    value_sha256: str
    source_file: str
    source_path: JsonPath
    source_span: Span | None
    target_file: str
    target_span: Span
    target_value: str
    quote: str | None
    basis: str
    evidence: str


def _target(value: str, source: SourceBinding, path: JsonPath, span: Span | None, *,
            target: SourceBinding, target_span: Span, target_value: str,
            basis: str, proof: str, quote: str | None = None) -> _Target:
    value_sha256 = _digest(value)
    occurrence = [basis, source.file, path, span]
    return _Target(
        "mz:" + _fingerprint(occurrence), value, value_sha256, source.file, path, span,
        target.file, target_span, target_value, quote, basis,
        _fingerprint([proof, occurrence, source.sha256, target.file, target.sha256, target_span, value_sha256]),
    )


def _locale_targets(snapshot: _Snapshot, proof: LocaleCatalogProof) -> tuple[str, list[_Target]]:
    snapshot.file(proof.loader).verify_span(proof.loader_evidence)
    if not proof.catalogs or type(proof.declared_complete) is not bool:
        raise TextInventoryError("Locale proof needs a source and target catalog binding")
    # Verify all loader, source, and target snapshots before enumerating any values.
    for catalog in proof.catalogs:
        snapshot.file(catalog.source)
        if isinstance(catalog.target, SourceBinding):
            snapshot.file(catalog.target)
    loader = [proof.loader.file, proof.loader.sha256, proof.loader_evidence.start,
              proof.loader_evidence.end, proof.loader_evidence.sha256]
    targets = []
    contracts = []
    for catalog in proof.catalogs:
        original = snapshot.json_document(catalog.source)
        if isinstance(catalog.target, NewLocaleCatalog):
            target = snapshot.create_catalog(catalog.target.file, catalog.source)
            destination = original
        else:
            target = catalog.target
            destination = snapshot.json_document(target)
        create = target.file in snapshot.new_files
        basis = _fingerprint([loader, catalog.source.file, target.file, create])
        contracts.append(_fingerprint([catalog.source.file, catalog.source.sha256, target.file,
                                       target.sha256, create, sorted(map(_fingerprint, catalog.values))]))
        for original_path, destination_path in catalog.values:
            value, _ = original.string(original_path)
            target_value, span = destination.string(destination_path)
            if value.strip():
                targets.append(_target(value, catalog.source, original_path, None, target=target,
                                       target_span=span, target_value=target_value,
                                       basis="locale_catalog", proof=basis))
    return _fingerprint([loader, sorted(contracts)]), targets


def _display_targets(snapshot: _Snapshot, observations: DisplayObservations) -> list[_Target]:
    targets = []
    for data in observations.data:
        document = snapshot.json_document(data.source)
        for path in _native_slots(document.root, data.kind):
            value, span = document.string(path)
            if value.strip():
                targets.append(_target(value, data.source, path, None, target=data.source,
                                       target_span=span, target_value=value,
                                       basis="format_field", proof=data.kind.value))
    for render in observations.scripts:
        source = snapshot.file(render.source)
        call_span = source.verify_span(render.call)
        receiver_span = source.verify_span(render.receiver.expression)
        argument_span = source.verify_span(render.argument)
        call = snapshot.script_calls(render.source).get(call_span)
        if (call is None or call.receiver != receiver_span or call.first_string != argument_span
                or call.arity not in _RENDER_ARITY.get((render.receiver.kind, call.method), ())):
            raise TextInventoryError("Unproven native MZ render argument")
        literal = source.text[slice(*argument_span)]
        value = _decode_js_string(literal)
        if value.strip():
            proof = _fingerprint([render.receiver.kind.value, receiver_span, call_span, call.method])
            targets.append(_target(value, render.source, (), argument_span, target=render.source,
                                   target_span=argument_span, target_value=value,
                                   basis="render_argument", proof=proof, quote=literal[0]))
    return targets


class TextInventory:
    """An eligible snapshot; apply returns only changed destination files."""

    def __init__(self, snapshot: _Snapshot, targets: list[_Target], *, route: str,
                 evidence: str, review: str, pending: int, complete: bool,
                 control_only: int = 0, unclassified: int = 0) -> None:
        self._targets = {target.id: target for target in targets}
        if len(self._targets) != len(targets):
            raise TextInventoryError("Duplicate source target evidence")
        spans: dict[str, list[Span]] = defaultdict(list)
        for target in targets:
            spans[target.target_file].append(target.target_span)
        for locations in spans.values():
            locations.sort()
            if any(end > start for (_, end), (start, _) in pairwise(locations)):
                raise TextInventoryError("Overlapping destination target evidence")
        self._files = {file: snapshot.files[file] for file in spans}
        self._new_file_counts = {file: len(spans[file]) for file in snapshot.new_files if file in spans}
        self._route = route
        self._evidence = evidence
        self._review = review
        self._pending = pending
        self._complete = complete
        self._control_only = control_only
        self._unclassified = unclassified
        self._scope_fingerprint = _fingerprint([
            evidence, review, pending, control_only, unclassified,
            sorted((file, source.sha256) for file, source in snapshot.files.items()),
            sorted(target.evidence for target in targets),
        ])

    def document(self, *, method: str, implementation_fingerprint: str) -> dict[str, Any]:
        """Materialize the Core source-inventory wire format independently of apply."""
        if not self._targets:
            raise TextInventoryError("A source inventory requires at least one eligible segment")
        locale = self._route == "locale_data"
        segments = []
        for target in self._targets.values():
            location: dict[str, Any] = {"file": target.source_file, "path": list(target.source_path)}
            if target.source_span is not None:
                location["span"] = list(target.source_span)
            segments.append({
                "id": target.id,
                "source": target.value,
                "metadata": {
                    "source_location": location,
                    "storage_role": "locale_value" if locale else "display_value",
                    "display_binding_id": target.id,
                    "display_scope_evidence": {
                        "kind": "locale_value" if locale else "positive_display_binding",
                        "binding_basis": target.basis,
                        "source_sha256": target.value_sha256,
                        "evidence_fingerprint": target.evidence,
                    },
                },
            })
        return {
            "schema": "parr0t.source-inventory.v1",
            "method": method,
            "implementation_fingerprint": implementation_fingerprint,
            "coverage": {
                "declared_complete": self._complete,
                "exclusions": [],
                "text_source": {
                    "route": self._route,
                    "locale_probe": "applicable" if locale else "unavailable",
                    "evidence_fingerprint": self._evidence,
                    "screen_review": self._review,
                    "pending_display_count": self._pending,
                },
                "display_scope": {
                    "schema": "parr0t.display-scope.v1",
                    "raw_occurrence_count": len(segments) + self._control_only + self._unclassified,
                    "display_candidate_count": len(segments),
                    "control_only_count": self._control_only,
                    "unclassified_count": self._unclassified,
                    "evidence_fingerprint": self._scope_fingerprint,
                },
            },
            "segments": segments,
        }

    def apply(self, translations: Mapping[str, str]) -> dict[str, bytes]:
        """Patch only bound value literals; unknown IDs fail closed."""
        edits: dict[str, list[tuple[Span, str]]] = defaultdict(list)
        creates: dict[str, int] = defaultdict(int)
        for target_id, translation in translations.items():
            target = self._targets.get(target_id)
            if target is None or not isinstance(translation, str) or not translation.strip():
                raise TextInventoryError("Translation must name an eligible target and supply nonempty text")
            if _PROTECTED_MZ_TEXT.findall(translation) != _PROTECTED_MZ_TEXT.findall(target.value):
                raise TextInventoryError("Translation changed protected MZ text tokens")
            create = target.target_file in self._new_file_counts
            if create:
                creates[target.target_file] += 1
            if translation == target.target_value:
                if create and target.target_file not in edits:
                    edits[target.target_file] = []
                continue
            try:
                translation.encode("utf-8")
            except UnicodeError as exc:
                raise TextInventoryError("Translation must be valid UTF-8") from exc
            literal = json.dumps(translation, ensure_ascii=target.quote is not None)
            if target.quote == "'":
                literal = "'" + literal[1:-1].replace("'", "\\'") + "'"
            edits[target.target_file].append((target.target_span, literal))
        if creates and (not self._complete or any(count != self._new_file_counts[file]
                                                  for file, count in creates.items())):
            raise TextInventoryError("A new locale catalog requires every bound value")
        result = {}
        for file, replacements in edits.items():
            source = self._files[file]
            fragments = []
            end = 0
            for (start, next_end), literal in sorted(replacements):
                fragments.extend((source.text[end:start], literal))
                end = next_end
            fragments.append(source.text[end:])
            result[file] = source.bom + "".join(fragments).encode("utf-8")
        return result


def materialize(*, read_source: Callable[[str], bytes],
                probe_locale: Callable[[], LocaleCatalogProof | LocaleUnavailable | LocaleInconclusive],
                load_display: Callable[[], DisplayObservations]) -> TextInventory:
    """Resolve locale applicability first; only proven absence admits display extraction."""
    finding = probe_locale()
    snapshot = _Snapshot(read_source)
    if isinstance(finding, LocaleCatalogProof):
        evidence, targets = _locale_targets(snapshot, finding)
        return TextInventory(snapshot, targets, route="locale_data", evidence=evidence,
                             review="not_applicable", pending=0, complete=finding.declared_complete)
    if not isinstance(finding, LocaleUnavailable):
        raise TextInventoryError("Locale probe is inconclusive; text source is blocked")
    if not isinstance(finding.evidence_fingerprint, str) or not _SHA256.fullmatch(finding.evidence_fingerprint):
        raise TextInventoryError("Locale unavailability requires bound probe evidence")
    observations = load_display()
    pending = observations.pending_display_count
    counts = (pending, observations.control_only_count, observations.unclassified_count)
    if (any(type(count) is not int or count < 0 for count in counts)
            or not isinstance(observations.screen_review, ScreenReview)
            or type(observations.declared_complete) is not bool):
        raise TextInventoryError("Invalid display review accounting")
    targets = _display_targets(snapshot, observations)
    review = observations.screen_review.value
    return TextInventory(snapshot, targets, route="display_extraction",
                         evidence=finding.evidence_fingerprint, review=review, pending=pending,
                         complete=observations.declared_complete and pending == 0,
                         control_only=observations.control_only_count, unclassified=observations.unclassified_count)
