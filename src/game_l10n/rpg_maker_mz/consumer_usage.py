"""Categorical cached JS usage evidence, not authority to exclude text.

Trace a deliberately closed subset: direct literal assignments, object entries,
conditional arms and same-scope uses of unambiguous local bindings. Public API
member names are hints unless the engine has separate receiver/type evidence.
No title-specific names, literal values, or source locations enter the report.
"""
from __future__ import annotations

from collections import Counter, defaultdict


_RENDER = frozenset({"fillText", "strokeText", "drawText", "drawTextEx", "setText", "addText", "addMessage"})
_RENDER_FIELDS = frozenset({"innerText", "textContent", "innerHTML", "placeholder", "title", "ariaLabel"})
_RESOURCE_FIELDS = frozenset({"src", "href", "url", "filename"})
_PRESENTATION_FIELDS = frozenset({"fillStyle", "strokeStyle", "shadowColor", "font", "backgroundColor"})
_CONTROL_CALLS = frozenset({"getContext", "createElement", "addEventListener", "removeEventListener", "setAttribute"})
_CONDITIONS = frozenset({"if", "while", "switch", "with"})
_DECLARATIONS = frozenset({"let", "const", "var"})
_ASSIGNMENT_PREFIX = frozenset({"=", "!", "<", ">", "+", "-", "*", "/", "%", "&", "|", "^", "?"})
_OPEN = {"(": ")", "{": "}", "[": "]"}
_CLOSE = frozenset(_OPEN.values())
_OPERATOR_RANK = {
    op: rank for rank, operators in enumerate((
        (",",), ("=", "+=", "-=", "*=", "/=", "%=", "&&=", "||=", "??=", "&=", "|=", "^=",
                  "**=", "<<=", ">>=", ">>>="), ("?",), ("||", "??"), ("&&",),
        ("|",), ("^",), ("&",), ("==", "!=", "===", "!=="),
        ("<", "<=", ">", ">=", "in", "instanceof"), ("<<", ">>", ">>>"),
        ("+", "-"), ("*", "/", "%"), ("**",),
    )) for op in operators
}
_PREFIX_OPERATORS = frozenset({"+", "-", "!", "~", "++", "--", "typeof", "void", "delete", "await"})
_EXPRESSION_OPERATORS = _OPERATOR_RANK.keys() | _PREFIX_OPERATORS | {"?."}
_MAX_PROPERTY_PATH = 8
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_STRING_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}
_FLOW_LIMIT = 128
_ALIAS, _ARGUMENT, _NESTED = 1, 2, 4
_FLOW_LABELS = ("direct_", "via_alias_", "via_function_", "via_alias_and_function_",
                "via_nested_scope_", "via_nested_scope_and_alias_", "via_nested_scope_and_function_",
                "via_nested_scope_alias_and_function_")
_FLOW_TERMINATORS = frozenset({";", ",", "}", ""})


def _field_kind(name):
    if name in _RENDER_FIELDS:
        return "display_field_shape"
    if name in _RESOURCE_FIELDS:
        return "resource_field_shape"
    if name in _PRESENTATION_FIELDS:
        return "presentation_field_shape"
    return "other_field_shape"


def _key(token):
    kind, value = token[:2]
    if kind == "name":
        return value
    if kind == "string" and value[:1] in {"\"", "'"} and "\\" not in value:
        return value[1:-1]
    return None


def _quoted_property_key(token):
    key = _key(token)
    if key is not None or token[0] != "string" or token[1][:1] not in {"'", '"'}:
        return key
    # Normalize JavaScript escapes, not Python escapes. Unsupported legacy
    # numeric escapes leave an unknown key rather than a false noncollision.
    raw = token[1]
    out, i, limit = [], 1, len(raw) - 1
    while i < limit:
        char = raw[i]
        i += 1
        if char != "\\":
            out.append(char)
            continue
        if i == limit:
            return None
        char = raw[i]
        i += 1
        if char == "u" and raw[i] == "{":
            close = raw.find("}", i + 1, limit)
            digits = raw[i + 1:close] if close >= 0 else ""
            if not 0 < len(digits) <= 6 or any(c not in _HEX_DIGITS for c in digits):
                return None
            point = int(digits, 16)
            if point > 0x10ffff:
                return None
            out.append(chr(point))
            i = close + 1
        elif char in {"x", "u"}:
            width = 2 if char == "x" else 4
            digits = raw[i:i + width]
            if i + width > limit or any(c not in _HEX_DIGITS for c in digits):
                return None
            out.append(chr(int(digits, 16)))
            i += width
        elif char in "\r\n\u2028\u2029":
            if char == "\r" and raw[i] == "\n":
                i += 1
        elif char.isascii() and char.isdigit() and (
                char != "0" or (raw[i].isascii() and raw[i].isdigit())):
            return None
        else:
            out.append(_STRING_ESCAPES.get(char, char))
    # JS compares UTF-16 code units; merge any escaped surrogate pairs.
    return "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


class _ObjectMembers:
    """Latest alternate property positions, indexed once per affected literal."""

    def __init__(self):
        self.known = {}
        self.dynamic = self.unknown = -1


class _ForwardIndex:
    def __init__(self):
        self.reads = defaultdict(dict)
        self.boundaries = defaultdict(dict)
        self.edges = defaultdict(list)
        self.property_writes = set()
        self.binding_writes = Counter()


class ConsumerUsage:
    """Index syntactic storage and actual read sites from a single cached token stream."""

    def __init__(self, tokens):
        self.tokens = tokens
        n = len(tokens)
        self.values = values = [token[1] for token in tokens]
        self.parent = parents = [-1] * n
        self.scope = scopes = [-1] * n
        self.objects = objects = {}
        self.ternary = ternary = set()
        self.colon_after_key = set()
        scope_parent = self.scope_parent = {}
        self.locals = declarations = Counter()
        self.writes = writes = Counter()
        self.reads = reads = defaultdict(set)
        self.close = closing = {}
        self.open = {}
        self.object_keys = object_keys = Counter()
        last_duplicate = self.last_duplicate = {}
        self.spread_frames = spread = set()
        self.arg_slot = slots = [-1] * n
        self.functions = functions = {}
        self.function_passes = passes = defaultdict(list)
        self.local_callables = {}
        self.arrow_parameters = Counter()
        self.forward_index = None
        self.baseline_forward_index = None
        self.index_writes = index_writes = Counter()
        self.index_reads = index_reads = defaultdict(dict)
        self.index_competing_writes = competing = defaultdict(set)
        self.index_addresses = addresses = {}
        self.index_rejections = index_rejections = {}
        self.computed_key_types = computed_key_types = {}
        self.computed_key_operators = computed_key_operators = {}
        self.path_reads = defaultdict(dict)
        self.path_writes = set()
        arrays = self.arrays = set()
        alternate_members = self.alternate_members = defaultdict(list)
        self.member_profiles = {}
        self.conditional_colons = {}
        self.bare_writes = bare_writes = Counter()
        self.immutable = immutable = set()
        params = set()
        bare_args = Counter()
        frames, questions = [], defaultdict(list)
        scope = -1
        for i, (kind, value, *_rest) in enumerate(tokens):
            parent = frames[-1][0] if frames else -1
            parents[i], scopes[i] = parent, scope
            if parent >= 0 and values[parent] in {"(", "["}:
                slots[i] = bare_args[parent]
                if value == ",":
                    bare_args[parent] += 1
            if value == "." and parent >= 0 and values[i + 1:i + 3] == [".", "."]:
                spread.add(parent)
            prev = values[i - 1] if i else ""
            following = values[i + 1] if i + 1 < n else ""
            if (parent in objects and value != "}" and (i - 1 == parent
                    or (prev == "," and parents[i - 1] == parent))
                    and (following != ":" or _key(tokens[i]) is None)):
                alternate_members[parent].append(i)
            if value in _OPEN:
                frames.append((i, scope))
                if value == "[" and (prev in {"=", "return", "(", "[", "?"} or
                        (prev == ":" and (i - 1 in self.colon_after_key or i - 1 in ternary)) or
                        (prev == "," and parent >= 0 and values[parent] in {"(", "["})):
                    arrays.add(i)
                if value == "{":
                    # Recognize expression-introduced objects, not arbitrary blocks.
                    is_object = prev in {"=", "return", "(", "[", "?"} or (
                        prev == ":" and (i - 1 in self.colon_after_key or i - 1 in ternary)
                    ) or (prev == "," and parent >= 0 and (parent in objects or values[parent] in {"(", "["}))
                    if is_object:
                        objects[i] = (scope, values[i - 2]) if (
                            i >= 3 and prev == "=" and tokens[i - 2][0] == "name" and values[i - 3] in _DECLARATIONS
                        ) else None
                    else:
                        scope_parent[i] = scope
                        scope = i
            elif value in _CLOSE:
                if frames and _OPEN[values[frames[-1][0]]] == value:
                    opener, scope = frames.pop()
                    closing[opener] = i
                    self.open[i] = opener
                    questions.pop(opener, None)
            elif value == ";":
                questions.pop(parent, None)
            elif value == "?" and prev != "?" and following not in {"?", "."}:
                # Used again when classifying the two arms of a computed key.
                questions[parent].append(i)
            elif value == ":":
                if questions.get(parent):
                    question = questions[parent].pop()
                    self.conditional_colons[question] = i
                    ternary.add(i)
                elif parent in objects and i >= 2 and parents[i - 1] == parent and _key(tokens[i - 1]) is not None and (
                    i - 2 == parent or (values[i - 2] == "," and parents[i - 2] == parent)
                ):
                    self.colon_after_key.add(i)
                    address = parent, _key(tokens[i - 1])
                    object_keys[address] += 1
                    if object_keys[address] > 1:
                        last_duplicate[address] = i
            if kind == "name" and prev in _DECLARATIONS:
                declarations[(scope, value)] += 1
                if prev == "const":
                    immutable.add((scope, value))

        # Function declarations and direct function-expression parameters. Only
        # closed positional signatures can participate in argument binding.
        for i, (kind, value, *_rest) in enumerate(tokens):
            if kind != "name" or value != "function":
                continue
            named = i + 1 < n and tokens[i + 1][0] == "name"
            opener = i + (2 if named else 1)
            end = closing.get(opener, -1)
            body = end + 1
            if end < 0 or body >= n or values[opener] != "(" or values[body] != "{":
                continue
            names = self._parameter_names(opener, end)
            if names is None:
                continue
            for name in names:
                params.add((body, name))
                declarations[body, name] += 1
            if named and (i == 0 or values[i - 1] in {";", "{", "}"}):
                function = (scopes[i], values[i + 1])
                functions[function] = (body, names) if function not in functions else None
            if self._const_initializer(i, closing.get(body)):
                function = (scopes[i], values[i - 2])
                self.local_callables[function] = i, body, names

        for i in range(1, n - 2):
            if values[i] != "=" or values[i + 1] != ">" or tokens[i][3] != tokens[i + 1][2] or values[i + 2] != "{":
                continue
            end, body = i - 1, i + 2
            start = parents[end] if values[end] == ")" else end
            if start < 0 or not self._const_initializer(start, closing.get(body)):
                continue
            if closing.get(start) == end:
                names = self._parameter_names(start, end)
            else:
                names = (values[start],) if tokens[start][0] == "name" and start == end else None
            if names is not None:
                function = (scopes[start], values[start - 2])
                self.local_callables[function] = start, body, names
                self.arrow_parameters.update((body, name) for name in names)

        for i, (kind, name, *_rest) in enumerate(tokens):
            if kind == "name" and (i == 0 or values[i - 1] != ".") and self._write_after(i + 1):
                bare_writes[scopes[i], name] += 1

        # Match a concrete indexed write to later reads of the same receiver
        # and key. A key may be a literal, const, or unreassigned parameter.
        for i, value in enumerate(values):
            if value != "[" or (end := closing.get(i)) is None:
                continue
            write = self._write_after(end + 1)
            address, reason = self._index_address(i, end, params)
            if address is None:
                if write:
                    index_rejections[end] = reason
                continue
            start, container, key = address
            if reason == "unsupported_key_expression":
                constant = self._constant_key(i + 1, end)
                if write:
                    computed_key_types[end] = ("constant_expression" if constant is not None
                                               else self._computed_key_shape(i + 1, end))
                    computed_key_operators[end] = self._operator_shape(i + 1, end)
                if constant is not None:
                    key, reason = ("literal", constant), "matched"
            if write:
                competing[container].add(key)
                if key is None:
                    index_rejections[end] = reason
                else:
                    addresses[end] = container, key
                    index_writes[container, key] += 1
            elif key is not None:
                index_reads[container, key][self._use_at(start, end + 1)] = start

        for i, (kind, name, *_rest) in enumerate(tokens):
            if kind != "name" or (i and values[i - 1] in {".", "?"}):
                continue
            if (scopes[i], name) not in declarations:
                continue
            frame = scopes[i]
            if i + 1 < n and values[i + 1] in {".", "["}:
                self._record_property_path(i)
            end, key, member_address = i + 1, None, None
            if i + 2 < n and values[i + 1] == "." and tokens[i + 2][0] == "name":
                end, key = i + 3, values[i + 2]
                # Dot and literal bracket access name the same JS property.
                container = frame, (name,)
                member_address = container, ("literal", key)
            elif i + 3 < n and values[i + 1] == "[" and values[i + 3] == "]" and tokens[i + 2][0] == "string":
                end, key = i + 4, _key(tokens[i + 2])
                if key is None:
                    continue
            ref = (frame, name, key)
            if self._write_after(end):
                writes[ref] += 1
                if member_address is not None:
                    container, member_key = member_address
                    competing[container].add(member_key)
                    index_writes[member_address] += 1
                continue
            if (i and values[i - 1] in _DECLARATIONS) or (end < n and values[end] == ":"):
                continue
            use = self._use_at(i, end)
            reads[ref].add(use)
            if member_address is not None:
                sites = index_reads[member_address]
                sites[use] = max(i, sites.get(use, -1))
            if key is None:
                target, reason = self._argument_target(i, end)
                if reason != "no_direct_argument":
                    passes[frame, name].append((i, target, reason))

    def _parameter_names(self, opener, end):
        signature = range(opener + 1, end)
        if len(signature) and (len(signature) % 2 == 0 or not all(
                self.tokens[j][0] == "name" if (j - opener) % 2 else self.values[j] == "," for j in signature)):
            return None
        names = tuple(self.values[j] for j in range(opener + 1, end, 2))
        return names if len(set(names)) == len(names) else None

    def _const_initializer(self, start, end):
        values = self.values
        return (start >= 3 and end is not None and values[start - 3] == "const" and values[start - 1] == "="
                and self.tokens[start - 2][0] == "name"
                and (end + 1 == len(values) or values[end + 1] in _FLOW_TERMINATORS))

    def _write_after(self, index):
        values = self.values
        if index >= len(values):
            return False
        op = values[index]
        after = values[index + 1] if index + 1 < len(values) else ""
        return ((op == "=" and after not in {"=", ">"})
                or (op in {"+", "-", "*", "/", "%", "&", "|", "^", "?"} and after == "=")
                or (op in {"+", "-"} and after == op))

    def _index_address(self, opening, closing, params):
        values, tokens = self.values, self.tokens
        if opening < 1 or tokens[opening - 1][0] != "name":
            return None, "unsupported_receiver_expression"
        start = opening - 1
        while start >= 2 and values[start - 1] == "." and tokens[start - 2][0] == "name":
            start -= 2
        if start and values[start - 1] in {".", "?"}:
            return None, "unsupported_receiver_expression"
        scope = self.scope[opening]
        receiver = (scope, values[start])
        container = scope, tuple(values[start:opening])
        if self.locals[receiver] != 1:
            return None, "receiver_not_unique_local"
        if self.bare_writes[receiver] > int(receiver not in params):
            return None, "receiver_reassigned"
        if closing - opening > 16:
            return (start, container, None), "unsupported_key_expression"
        key_parts = values[opening + 1:closing]
        if len(key_parts) == 1 and tokens[opening + 1][0] == "string":
            key = _key(tokens[opening + 1])
            if key is None:
                return (start, container, None), "unsupported_literal_key"
            index = ("literal", key)
        elif key_parts and all(item.isascii() and item.isdigit() for item in key_parts):
            # Normalize numeric property keys, not legacy octal or split literals.
            number = "".join(key_parts)
            if (len(number) > 15 or (len(number) > 1 and number.startswith("0"))
                    or (len(key_parts) > 1 and (
                        len(number) != len(key_parts) or tokens[closing - 1][3] - tokens[opening + 1][2] != len(number)
                    ))):
                return (start, container, None), "unsupported_literal_key"
            index = ("literal", number)
        elif len(key_parts) == 1 and tokens[opening + 1][0] == "name":
            symbol = (scope, key_parts[0])
            if self.locals[symbol] != 1 or not (
                (symbol in self.immutable and self.bare_writes[symbol] == 1)
                or (symbol in params and not self.bare_writes[symbol])
            ):
                return (start, container, None), "key_not_stable_local"
            index = ("binding", key_parts[0])
        else:
            return (start, container, None), "unsupported_key_expression"
        return (start, container, index), "matched"

    def _constant_key(self, start, end):
        """Fold a closed JS fragment: grouped literals and binary plus only."""
        if not 0 < end - start <= 24:
            return None
        values, tokens = self.values, self.tokens

        def expression(i, limit):
            def operand(j):
                if j >= limit:
                    return None, j
                if values[j] == "(":
                    closing = self.close.get(j, limit)
                    if not j < closing < limit:
                        return None, j
                    value, stop = expression(j + 1, closing)
                    return (value, closing + 1) if stop == closing else (None, j)
                if tokens[j][0] == "string":
                    # Only ordinary, unescaped string literals count.
                    return _key(tokens[j]), j + 1
                k = j
                while (k < limit and values[k].isascii() and values[k].isdigit()
                       and (k == j or tokens[k - 1][3] == tokens[k][2] and len(values[k]) == len(values[k - 1]) == 1)):
                    k += 1
                number = "".join(values[j:k])
                if not number or (len(number) > 1 and number[0] == "0") or len(number) > 15:
                    return None, j
                return int(number), k

            value, i = operand(i)
            while value is not None and i < limit and values[i] == "+":
                right, i = operand(i + 1)
                if right is None:
                    return None, i
                if isinstance(value, str) or isinstance(right, str):
                    value = str(value) + str(right)
                else:
                    value += right
                    if value > 9007199254740991:
                        return None, i
            return value, i

        value, stop = expression(start, end)
        return str(value) if value is not None and stop == end else None

    def _computed_key_shape(self, start, end):
        """Partition rejected keys by visible syntax, not inferred semantics."""
        tokens, values = self.tokens, self.values
        member = subscript = operation = grouped = False
        for i in range(start, end):
            kind, token = tokens[i][:2]
            if kind == "dynamic" or (kind == "string" and token.startswith("`")):
                return "template_literal"
            if kind != "punct":
                continue
            if token == "(":
                if i > start and (tokens[i - 1][0] == "name" or values[i - 1] in {")", "]"}):
                    return "call_expression"
                grouped = True
            elif token == ".":
                member = True
            elif token == "[":
                subscript = True
            elif token in _ASSIGNMENT_PREFIX or token in {":", "~"}:
                operation = True
        if operation:
            return "operator_expression"
        if subscript:
            return "nested_subscript"
        if member:
            return "member_access"
        return "grouped_expression" if grouped else "other_computed_expression"

    def previously_unsupported_key_type(self, source_index):
        """Key category for an indexed write rejected by the original grammar."""
        return self.computed_key_types.get(source_index - 2)


    def previously_unsupported_key_operator(self, source_index):
        """Outermost operator and operand categories; never returns source tokens."""
        return self.computed_key_operators.get(source_index - 2)

    def _operator_shape(self, start, end):
        values, tokens = self.values, self.tokens
        while start < end and values[start] == "(" and self.close.get(start) == end - 1:
            start, end = start + 1, end - 1
        candidate, unary = None, None
        i, expect_operand = start, True
        while i < end:
            kind, value = tokens[i][:2]
            if kind == "punct" and value in _OPEN:
                i = self.close.get(i, end - 1) + 1
                expect_operand = False
                continue
            j, op = i + 1, value
            if kind == "punct" and value in _EXPRESSION_OPERATORS:
                # The cache lexer splits punctuators. Only touching spans form one operator.
                for stop in range(j, min(i + 4, end)):
                    if tokens[stop][0] != "punct" or tokens[stop - 1][3] != tokens[stop][2]:
                        break
                    joined = "".join(values[i:stop + 1])
                    if joined in _EXPRESSION_OPERATORS:
                        op, j = joined, stop + 1
            elif kind != "name" or value not in {"in", "instanceof", "typeof", "void", "delete", "await"}:
                op = None
            if op in _PREFIX_OPERATORS and expect_operand:
                if i == start:
                    unary = op, j
            elif op in _OPERATOR_RANK:
                rank = _OPERATOR_RANK[op]
                if ((candidate is None or rank < candidate[0] or
                        (rank == candidate[0] and op not in {"?", "**"} and rank != 1))
                        and not (rank == 1 and candidate is not None and candidate[1] == "?")):
                    candidate = rank, op, i, j
                if op == "?" and i in self.conditional_colons:
                    j = self.conditional_colons[i] + 1
                expect_operand = True
            elif op not in {"++", "--"}:
                expect_operand = value in {".", ":"} or op == "?."
            i = j
        if candidate is not None:
            _, op, pivot, right = candidate
            if op == "?":
                colon = min(self.conditional_colons.get(pivot, end), end)
                return ("?:", self._operand_shape(start, pivot), self._operand_shape(right, colon),
                        self._operand_shape(colon + 1, end))
            return op, self._operand_shape(start, pivot), self._operand_shape(right, end)
        if unary is not None:
            op, right = unary
            return "prefix_" + op, "none", self._operand_shape(right, end)
        if end - start >= 3 and values[end - 2] == values[end - 1] and values[end - 1] in {"+", "-"} and (
                tokens[end - 2][3] == tokens[end - 1][2]):
            return "postfix_" + values[end - 1] * 2, self._operand_shape(start, end - 2), "none"
        return "unrecognized", self._operand_shape(start, end), "none"

    def _operand_shape(self, start, end):
        if start >= end:
            return "none"
        tokens, values = self.tokens, self.values
        kind, first = tokens[start][:2]
        if end == start + 1:
            if kind == "string":
                return "string_literal"
            if kind == "dynamic":
                return "template_literal"
            if kind == "name":
                if first in {"true", "false", "null"}:
                    return "primitive_literal"
                return "local_name" if self.locals[self.scope[start], first] == 1 else "other_name"
        if first == "(" and self.close.get(start) == end - 1:
            return "grouped"
        if all(values[i].isascii() and values[i].isdigit()
               and (i == start or tokens[i - 1][3] == tokens[i][2]) for i in range(start, end)):
            return "decimal_integer"
        if kind == "name":
            stop, path = self._member_path(start, end)
            if stop == end and path:
                return "member_access"
            if stop < end and values[stop] == "(" and self.close.get(stop) == end - 1:
                return "call"
        return "expression"


    def _member_path(self, start, limit):
        values, tokens = self.values, self.tokens
        i, path = start + 1, []
        while i < limit:
            if values[i] == "." and i + 1 < limit and tokens[i + 1][0] == "name":
                key, i = values[i + 1], i + 2
            elif values[i] == "[" and i not in self.arrays and (close := self.close.get(i, limit)) < limit:
                key, i = self._constant_key(i + 1, close), close + 1
            else:
                break
            if len(path) < _MAX_PROPERTY_PATH:
                path.append(key)
            elif len(path) == _MAX_PROPERTY_PATH:
                path.append(None)
        return i, tuple(path)

    def _record_property_path(self, start):
        values = self.values
        end, path = self._member_path(start, len(values))
        if not path:
            return
        address = self.scope[start], values[start], path
        prefix_write = start > 0 and (
            values[start - 1] == "delete" or (
                start > 1 and values[start - 2] == values[start - 1] and values[start - 1] in {"+", "-"}))
        if prefix_write or self._write_after(end) or self._compound_path_write(end):
            self.path_writes.add(address)
        elif None not in path and not (end < len(values) and values[end] in {":", "?"}):
            self.path_reads[address][self._use_at(start, end)] = start

    def _compound_path_write(self, start):
        tokens = self.tokens
        for length in (3, 4):
            end = start + length
            if end > len(tokens) or tokens[end - 1][1] != "=":
                continue
            if all(tokens[i][0] == "punct" and (i == start or tokens[i - 1][3] == tokens[i][2])
                   for i in range(start, end)):
                if "".join(self.values[start:end]) in {"&&=", "||=", "??=", "**=", "<<=", ">>=", ">>>="}:
                    return True
        return False

    def _path_trace(self, owner, path, earliest):
        if self.locals[owner] != 1 or self.bare_writes[owner] > int(earliest >= 0):
            return "object_binding_reassigned"
        for length in range(len(path)):
            prefix = path[:length]
            if ((*owner, prefix + (path[length],)) in self.path_writes
                    or (*owner, prefix + (None,)) in self.path_writes):
                return "property_path_reassigned"
        uses = self.path_reads.get((*owner, path), {})
        kinds = {kind for kind, pos in uses.items() if pos > earliest}
        return next(iter(kinds)) if len(kinds) == 1 else "mixed_reads" if kinds else "no_later_path_read"

    def _member_name(self, start, limit):
        """Read a static or computed object property name, not its value."""
        if start >= limit:
            return None, start
        token = self.tokens[start]
        if token[1] == "[":
            end = self.close.get(start, limit)
            if end >= limit:
                return None, start
            if end == start + 2:
                operand = self.tokens[start + 1]
                if operand[0] == "string":
                    return _quoted_property_key(operand), end + 1
                if operand[1] in {"true", "false", "null"}:
                    return operand[1], end + 1
            return self._constant_key(start + 1, end), end + 1
        if token[0] in {"name", "string"}:
            return _quoted_property_key(token), start + 1
        end = start
        while (end < limit and self.values[end].isascii() and self.values[end].isdigit()
               and (end == start or self.tokens[end - 1][3] == self.tokens[end][2])):
            end += 1
        return self._constant_key(start, end), end

    def _alternate_member(self, start, end):
        """Return a known name or a bounded uncertainty class for an object entry."""
        values = self.values
        def method_at(pos):
            stop = self.close.get(pos, end)
            return values[pos] == "(" and stop + 1 < end and values[stop + 1] == "{"

        name_start = start + int(values[start] == "*")
        name, stop = self._member_name(name_start, end)
        name_form = stop > name_start
        if name_form and (
                (start == name_start and values[stop] == ":")
                or method_at(stop)
                or (start == name_start and self.tokens[start][0] == "name" and values[stop] in {",", "}"})):
            return name, "dynamic" if values[name_start] == "[" else "unknown"
        if values[start] in {"get", "set", "async"}:
            name_start = start + 1
            if values[start] == "async" and values[name_start] == "*":
                name_start += 1
            name, stop = self._member_name(name_start, end)
            if stop > name_start and method_at(stop):
                return name, "dynamic" if values[name_start] == "[" else "unknown"
        return None, "unknown"

    def _literal_overwrite(self, root, marker, key):
        if key == "__proto__":
            return "prototype_setter_unresolved", True
        entries = self.alternate_members.get(root)
        duplicate = self.last_duplicate.get((root, key), -1)
        legacy_blocked = duplicate >= 0 or bool(entries)
        if duplicate > marker:
            return "later_same_key", legacy_blocked
        if not entries or entries[-1] < marker:
            return None, legacy_blocked
        profile = self.member_profiles.get(root)
        if profile is None:
            profile = self.member_profiles[root] = _ObjectMembers()
            limit = self.close.get(root, len(self.values) - 1)
            for start in entries:
                name, uncertainty = self._alternate_member(start, limit)
                if name is not None:
                    profile.known[name] = start
                elif uncertainty == "dynamic":
                    profile.dynamic = start
                else:
                    profile.unknown = start
        if profile.known.get(key, -1) > marker:
            return "later_same_key", legacy_blocked
        if profile.dynamic > marker:
            return "later_dynamic_key", legacy_blocked
        if profile.unknown > marker:
            return "later_unresolved_member", legacy_blocked
        return None, legacy_blocked

    def object_value_usage(self, source_index):
        """Return container, site, read shape, and overwrite evidence for previously blocked paths."""
        colon = source_index - 1
        if colon not in self.colon_after_key:
            return None
        obj = self.parent[colon]
        shape = ("nested_property" if obj - 1 in self.colon_after_key else
                 "array_element" if self.parent[obj] in self.arrays else "direct_object")
        root, path, prior_blocked, reason = self._object_path(obj, colon)
        if path is None:
            site = "ambiguous_property" if reason in {
                "later_same_key", "later_dynamic_key", "later_unresolved_member", "prototype_setter_unresolved",
            } else reason
            return shape, site, "unbound", reason if prior_blocked else None
        return shape, *self._object_trace(root, path, source_index), "no_later_key_conflict" if prior_blocked else None

    def _object_path(self, obj, colon):
        values = self.values
        route, root = [], obj
        marker = colon
        prior_blocked = False
        while len(route) < _MAX_PROPERTY_PATH:
            parent = self.parent[root]
            prev = values[root - 1] if root else ""
            end = self.close.get(root)
            if root in self.spread_frames:
                return root, None, prior_blocked, "spread_container"
            if marker is not None:
                key = _key(self.tokens[marker - 1])
                reason, was_blocked = self._literal_overwrite(root, marker, key)
                prior_blocked |= was_blocked
                if reason:
                    return root, None, prior_blocked, reason
                route.append(key)
            if end is None:
                return root, None, prior_blocked, "unclosed_container"
            after = values[end + 1] if end + 1 < len(values) else ""
            if (root - 1 in self.colon_after_key and parent in self.objects
                    and after in {",", "}"}):
                root, marker = parent, root - 1
                continue
            if (parent in self.arrays and prev in {"[", ","}
                    and after in {",", "]"}):
                route.append(str(self.arg_slot[root]))
                root, marker = parent, None
                continue
            return root, tuple(reversed(route)), prior_blocked, None
        return root, None, prior_blocked, "property_path_too_deep"

    def _object_trace(self, root, path, source_index):
        values = self.values
        prev = values[root - 1] if root else ""
        end = self.close[root]
        after = values[end + 1] if end + 1 < len(values) else ""
        if prev == "=":
            if (root >= 3 and self.tokens[root - 2][0] == "name"
                    and values[root - 3] in _DECLARATIONS and after in {";", ",", ")", "}", ""}):
                owner = self.scope[root], values[root - 2]
                direct = self._path_trace(owner, path, source_index)
                if direct != "no_later_path_read":
                    return "local_initializer", direct
                traces = {result for pos, target, _reason in self.function_passes.get(owner, ())
                          if pos > source_index and target is not None
                          and (result := self._path_trace(target, path, -1)) not in {
                              "object_binding_reassigned", "property_path_reassigned", "no_later_path_read"}}
                if traces:
                    use = next(iter(traces)) if len(traces) == 1 else "mixed_reads"
                    return "local_initializer", "via_function_" + use
                return "local_initializer", direct
            member = root >= 3 and (values[root - 2] == "]" or values[root - 3] == ".")
            return ("member_assignment" if member else "assignment"), "unbound"
        if prev == "return":
            return "return_value", "unbound"
        if prev == "?" or root - 1 in self.ternary:
            return "conditional_value", "unbound"
        target, reason = self._argument_target(root, end + 1)
        if target is not None:
            return "bound_call_argument", self._path_trace(target, path, -1)
        if reason != "no_direct_argument":
            return reason, "unbound"
        return "other_expression", "unbound"

    def local_initializer_forwarding(self, source_index, *, follow_nested=True):
        """Follow exact paths through aliases, positional calls, and admitted lexical scopes."""
        colon = source_index - 1
        if colon not in self.colon_after_key:
            return None
        root, path, _blocked, _reason = self._object_path(self.parent[colon], colon)
        if path is None or self._object_trace(root, path, source_index)[0] != "local_initializer":
            return None
        field = "forward_index" if follow_nested else "baseline_forward_index"
        index = getattr(self, field)
        if index is None:
            index = self._build_forward_index(follow_nested)
            setattr(self, field, index)
        return self._follow_reference(index, (self.scope[root], self.values[root - 2]), path, self.close[root])

    def _visible_callable(self, scope, name, call, writes):
        while True:
            owner = scope, name
            count = self.locals[owner] + self.arrow_parameters[owner]
            if owner in self.local_callables and count == 1 and writes[owner] == 1:
                start, body, names = self.local_callables[owner]
                if self.close[body] < call:
                    return (body, names), "bound_argument"
                return None, "call_before_callable_initializer"
            if count or writes[owner]:
                return None, "callee_shadowed_or_reassigned"
            if owner in self.functions or scope < 0:
                return self.functions.get(owner), "no_unique_supported_local_callee"
            scope = self.scope_parent[scope]

    def _visible_local(self, scope, name):
        while True:
            owner = scope, name
            if self.locals[owner] or self.arrow_parameters[owner]:
                return owner
            if scope < 0 or owner in self.functions:
                return None
            scope = self.scope_parent[scope]

    def _nested_scope_index(self, writes):
        """Index trusted block boundaries and direct activations in the declaring scope."""
        values, tokens = self.values, self.tokens
        bodies = {target[0] for target in self.functions.values() if target is not None}
        bodies.update(body for _start, body, _names in self.local_callables.values())
        activations = {}
        for body in self.scope_parent:
            if body not in self.close:
                continue
            prev = values[body - 1] if body else ""
            head = self.open.get(body - 1, 0) - 1
            if self.parent[body] not in self.objects and (
                    prev in {"else", "try", "finally", ";", "{", ""} or (
                        prev == ")" and head >= 0 and values[head] in {"if", "while", "for", "switch"})):
                activations[body] = None  # Same execution as the parent scope.
            if prev == ")" and head >= 0 and values[head] == "function":
                grouping, end = self.parent[head], self.close[body]
                if (grouping >= 0 and values[grouping] == "(" and head == grouping + 1
                        and (grouping == 0 or values[grouping - 1] in
                             {"=", "(", "[", ",", ";", "{", "}", ":", "return", "!", "void"})):
                    # Both (function () {...})() and (function () {...}()).
                    call = end + 2 if self.close.get(grouping) == end + 1 else end + 1
                    close = self.close.get(call)
                    if close is not None and values[call] == "(" and (
                            call == end + 2 or self.close.get(grouping) == close + 1):
                        if self._parameter_names(head + 1, body - 1) is not None:
                            bodies.add(body)
                            activations[body] = call
        # The existing local index intentionally binds only simple declarations.
        # Fence complex declarations and hoisted block vars before crossing scopes.
        opaque = set()
        hoisted = set()
        expression_arrows = {}
        for start, value in enumerate(values):
            if (value in {"with", "eval"} and start + 1 in self.close and values[start + 1] == "("
                    and (start == 0 or values[start - 1] != ".")):
                opaque.add(self.scope[start])
            if (value == "=" and start + 2 < len(values) and values[start + 1] == ">"
                    and tokens[start][3] == tokens[start + 1][2] and values[start + 2] != "{"):
                parent = self.parent[start]
                end = start + 2
                while end < len(values) and self.parent[end] == parent and values[end] not in {",", ";", "}"}:
                    end = self.close.get(end, end) + 1
                expression_arrows[start] = end
        for start, value in enumerate(values):
            if tokens[start][0] == "name" and value in {"class", "function"} and (
                    start == 0 or values[start - 1] in {";", "{", "}", "async", "export", "default"}):
                head = start + (2 if values[start + 1:start + 2] == ["*"] else 1)
                if head < len(values) and tokens[head][0] == "name":
                    owner = self.scope[start], values[head]
                    if value == "class" or self.functions.get(owner) is None:
                        hoisted.add(owner)
            if value not in _DECLARATIONS or (start and values[start - 1] == "."):
                continue
            name = start + 1
            if name >= len(values) or (tokens[name][0] != "name" and values[name] not in {"{", "["}):
                continue
            scope = self.scope[start]
            frame = scope
            while frame >= 0 and frame not in bodies and activations.get(frame, -1) is None:
                frame = self.scope_parent[frame]
            while name < len(values):
                if tokens[name][0] != "name":
                    opaque.add(scope)
                    if value == "var":
                        opaque.add(frame)
                    if name not in self.close:
                        break
                    name = self.close[name]
                elif value == "var" and frame != scope:
                    hoisted.add((frame, values[name]))
                parent = self.parent[start]
                j = name + 1
                while j < len(values) and self.parent[j] == parent and values[j] not in {",", ";", "}"}:
                    if j in self.close:
                        j = self.close[j]
                    j += 1
                if j >= len(values) or values[j] != "," or self.parent[j] != parent:
                    break
                name = j + 1
                if name < len(values) and tokens[name][0] == "name":
                    hoisted.add((frame if value == "var" else scope, values[name]))
        arrow_ranges = iter(expression_arrows.items())
        arrow_start, arrow_end = next(arrow_ranges, (len(values), -1))
        opaque_until = -1
        for start, (kind, name, *_span) in enumerate(tokens):
            call = start + 1
            if (kind != "name" or call not in self.close or values[call] != "("
                    or (start and values[start - 1] in {".", "?", "new", "function", "*", "class"})):
                continue
            scope = self.scope[start]
            while start > arrow_start:
                opaque_until = max(opaque_until, arrow_end)
                arrow_start, arrow_end = next(arrow_ranges, (len(values), -1))
            if start < opaque_until or scope in opaque or (scope, name) in hoisted:
                continue
            target, _reason = self._visible_callable(scope, name, call, writes)
            if target is not None and self.scope_parent[target[0]] == scope:
                activations[target[0]] = call
        return activations, opaque, hoisted, expression_arrows

    def _nested_reference(self, scope, owner, start, scopes):
        activations, opaque, hoisted, _arrows = scopes
        frame, name = owner
        trigger = start
        while True:
            if scope in opaque or (scope, name) in hoisted:
                return None, "nested_binding_ambiguous"
            if scope == frame:
                return trigger, None
            if scope not in activations:
                return None, "nested_activation_unresolved"
            if activations[scope] is not None:
                trigger = activations[scope]
            scope = self.scope_parent[scope]

    def _build_forward_index(self, follow_nested):
        values, tokens = self.values, self.tokens
        index = _ForwardIndex()
        references, pending_calls = [], []
        for start, (kind, name, *_span) in enumerate(tokens):
            if kind != "name" or (start and values[start - 1] == "."):
                continue
            scope = self.scope[start]
            owner = self._visible_local(scope, name)
            prev = values[start - 1] if start else ""
            if owner is None and start + 1 < len(values) and (
                    self._write_after(start + 1) or self._compound_path_write(start + 1)):
                # An assignment can also replace a hoisted declaration.
                frame = scope
                while (frame, name) not in self.functions and frame >= 0:
                    frame = self.scope_parent[frame]
                if (frame, name) in self.functions:
                    index.binding_writes[frame, name] += 1
                continue
            if owner is None or prev in {"function", "class"}:
                continue
            end, path = self._member_path(start, len(values))
            after = values[end] if end < len(values) else ""
            address = owner, path
            updating = (prev == "delete" or (
                start >= 2 and prev in {"+", "-"} and values[start - 2] == prev
                and tokens[start - 2][3] == tokens[start - 1][2]))
            if updating or self._write_after(end) or self._compound_path_write(end):
                if path:
                    index.property_writes.add(address)
                else:
                    index.binding_writes[owner] += 1
                continue
            if prev in _DECLARATIONS or after == ":":
                continue
            references.append((start, end, owner, path, prev, after))
        scopes = self._nested_scope_index(index.binding_writes) if follow_nested else None
        arrow_ranges = iter(scopes[3].items()) if scopes else iter(())
        arrow_start, arrow_end = next(arrow_ranges, (len(values), -1))
        opaque_until = -1
        for start, end, owner, path, prev, after in references:
            scope = self.scope[start]
            address = owner, path
            prefix = path[:path.index(None)] if None in path else path
            pos, mode = start, 0
            while start > arrow_start:
                opaque_until = max(opaque_until, arrow_end)
                arrow_start, arrow_end = next(arrow_ranges, (len(values), -1))
            if owner[0] != scope:
                if not follow_nested:
                    index.boundaries[owner, prefix]["nested_scope_reference"] = start
                    continue
                if start < opaque_until:
                    index.boundaries[owner, prefix]["nested_binding_ambiguous"] = start
                    continue
                parent = self.parent[start]
                while parent >= 0 and self.scope[parent] == scope:
                    close = self.close.get(parent)
                    if close is not None and values[parent] in {"{", "["} and self._write_after(close + 1):
                        index.boundaries[owner, prefix]["nested_binding_ambiguous"] = start
                        break
                    parent = self.parent[parent]
                else:
                    parent = -1
                if parent >= 0:
                    continue
                pos, reason = self._nested_reference(scope, owner, start, scopes)
                if reason is not None:
                    index.boundaries[owner, prefix][reason] = start
                    continue
                index.boundaries[owner, prefix]["nested_scope_forward"] = pos
                mode = _NESTED
            if prefix != path or (after == "?" and end + 1 < len(values) and values[end + 1] == "."):
                index.boundaries[owner, prefix]["dynamic_or_optional_access"] = pos
            elif (start >= 3 and values[start - 3] in _DECLARATIONS
                    and tokens[start - 2][0] == "name" and prev == "=" and after in _FLOW_TERMINATORS):
                index.edges[address].append((pos, (scope, values[start - 2]), mode | _ALIAS, start))
            elif (paren := self.parent[start]) >= 0 and values[paren] == "(" and prev in {"(", ","} and after in {")", ","}:
                pending_calls.append((address, start, end, pos, mode))
            else:
                if after == "(":
                    index.boundaries[owner, path[:-1]]["call_on_reference"] = pos
                if prev == "return":
                    index.boundaries[address]["returned_reference"] = pos
                elif prev == "=":
                    index.boundaries[address]["assigned_reference"] = pos
                elif not path:
                    index.boundaries[address]["bare_reference"] = pos
                key = mode, self._use_at(start, end)
                index.reads[address][key] = max(pos, index.reads[address].get(key, -1))
        for address, start, end, pos, mode in pending_calls:
            target, reason = self._argument_target(start, end, flow=index)
            if target is not None:
                index.edges[address].append((pos, target, mode | _ARGUMENT, -1))
            else:
                category = "grouped_or_conditional_reference" if reason == "no_direct_argument" else reason
                index.boundaries[address][category] = pos
                key = mode, self._use_at(start, end)
                index.reads[address][key] = max(pos, index.reads[address].get(key, -1)) if follow_nested else pos
        return index

    def _follow_reference(self, index, owner, path, earliest):
        queue = [(owner, path, earliest, 0)]
        seen, hits, boundaries = set(), set(), set()
        while queue:
            state = queue.pop()
            if state in seen:
                continue
            if len(seen) >= _FLOW_LIMIT:
                return "forwarding_limit", tuple(sorted(boundaries))
            seen.add(state)
            owner, path, earliest, mode = state
            expected_writes = int(earliest >= 0)
            if (self.locals[owner] + self.arrow_parameters[owner] != 1
                    or index.binding_writes[owner] != expected_writes):
                return "binding_not_single_assignment", tuple(sorted(boundaries))
            for i in range(len(path) + 1):
                prefix, rest = path[:i], path[i:]
                address = owner, prefix
                if i and (address in index.property_writes
                          or (owner, (*prefix[:-1], None)) in index.property_writes):
                    return "forwarded_property_reassigned", tuple(sorted(boundaries))
                boundaries.update(reason for reason, pos in index.boundaries.get(address, {}).items() if pos > earliest)
                for pos, target, edge, target_start in index.edges.get(address, ()):
                    if pos > earliest:
                        boundaries.add("alias_forward" if edge & _ALIAS else "function_forward")
                        queue.append((target, rest, target_start, mode | edge))
                reads = index.reads.get(address, {})
                if rest:
                    if prefix and any(pos > earliest for pos in reads.values()):
                        boundaries.add("prefix_read_without_transfer")
                else:
                    hits.update(_FLOW_LABELS[mode | read_mode] + kind
                                for (read_mode, kind), pos in reads.items()
                                if mode | read_mode and pos > earliest)
        if hits:
            outcome = next(iter(hits)) if len(hits) == 1 else "mixed_forwarded_reads"
        else:
            outcome = "no_exact_forwarded_read"
        return outcome, tuple(sorted(boundaries))

    def _argument_target(self, start, end, flow=None):
        values, tokens = self.values, self.tokens
        paren = self.parent[start]
        if (paren < 1 or values[paren] != "("
                or values[start - 1] not in {"(", ","} or end >= len(values) or values[end] not in {",", ")"}):
            return None, "no_direct_argument"
        if paren in self.spread_frames:
            return None, "spread_arguments"
        prev = values[paren - 2] if paren >= 2 else ""
        if prev in {".", "?"}:
            return None, "member_or_optional_call"
        if prev in {"new", "function"} or tokens[paren - 1][0] != "name":
            return None, "unsupported_callee_expression"
        name = values[paren - 1]
        if name in _CONDITIONS:
            return None, "no_direct_argument"
        scope = self.scope[start]
        if flow is None:
            callee = scope, name
            if self.locals[callee] or self.bare_writes[callee]:
                return None, "callee_shadowed_or_reassigned"
            target = self.functions.get(callee)
        else:
            target, reason = self._visible_callable(scope, name, paren, flow.binding_writes)
            if target is None:
                return None, reason
        if target is None:
            return None, "no_unique_supported_local_callee"
        body, names = target
        slot = self.arg_slot[start]
        if 0 <= slot < len(names):
            return (body, names[slot]), "bound_argument"
        return None, "parameter_slot_missing"

    def _function_trace(self, attempts, key):
        found, reasons = set(), set()
        for target, reason in attempts:
            if target is None:
                reasons.add(reason)
                continue
            owner = target
            if self.locals[owner] != 1 or self.bare_writes[owner] or self.writes[(*owner, key)]:
                reasons.add("parameter_or_property_reassigned")
                continue
            reads = self.reads.get((*owner, key))
            if reads:
                found.update(reads)
            else:
                reasons.add("no_parameter_property_read")
        if found:
            label = next(iter(found)) if len(found) == 1 else "mixed_reads"
            return "via_function_" + label, "matched"
        if not reasons:
            return None, "no_direct_argument"
        return None, next(iter(reasons)) if len(reasons) == 1 else "multiple_rejections"

    def _indexed_trace(self, closing, source_index):
        address = self.index_addresses.get(closing)
        if address is None:
            return None, self.index_rejections.get(closing, "index_write_not_recognized")
        container, key = address
        other = self.index_competing_writes[container]
        if self.index_writes[address] != 1 or any(
            item != key and (item is None or key[0] == "binding" or item[0] == "binding") for item in other
        ):
            return "ambiguous_write", "competing_index_writes"
        uses = {kind for kind, pos in self.index_reads.get(address, {}).items() if pos > source_index}
        if not uses:
            return None, "no_later_read" if address in self.index_reads else "no_same_address_read"
        label = next(iter(uses)) if len(uses) == 1 else "mixed_reads"
        return "indexed_lookup_" + label, "matched"

    def _use_at(self, start, end):
        values = self.values
        parent = self.parent[start]
        prev = values[start - 1] if start else ""
        after = values[end] if end < len(values) else ""
        if parent >= 0 and values[parent] == "[" and prev == "[" and after == "]":
            # A subscript has a receiver; an array literal doesn't.
            if parent and ((self.tokens[parent - 1][0] == "name"
                            and values[parent - 1] not in {"return", "throw", "yield", "case"})
                           or values[parent - 1] in {")", "]"}):
                return "index_read"
        if parent > 0 and values[parent] == "(" and prev in {"(", ","} and after in {",", ")"}:
            callee = values[parent - 1]
            if callee in _CONDITIONS:
                return "condition_read"
            if self.tokens[parent - 1][0] == "name":
                if parent >= 3 and values[parent - 2] == ".":
                    if callee in _RENDER:
                        return "display_call_shape"
                    if callee in _CONTROL_CALLS:
                        return "control_call_shape"
                return "other_call_read"
        if prev in {"=", "!", "<", ">"} or after in {"=", "!", "<", ">"}:
            return "expression_read"
        return "other_read"

    def _trace(self, owner, key, source_index, *, own_write):
        if owner is None or self.locals[owner] != 1:
            return "unbound", "object_binding_not_unique"
        if self.writes[(*owner, None)] > 1 or self.writes[(*owner, key)] > own_write:
            return "ambiguous_write", "object_or_property_reassigned"
        reason = "no_direct_argument"
        if key is not None:
            calls = ((target, outcome) for pos, target, outcome in self.function_passes.get(owner, ())
                     if pos > source_index)
            trace, reason = self._function_trace(calls, key)
            if trace is not None:
                return trace, reason
        reads = self.reads.get((*owner, key), ())
        if not reads:
            return "no_direct_read", reason
        return (next(iter(reads)) if len(reads) == 1 else "mixed_reads"), reason

    def classify(self, index, cohort):
        return self.classify_with_reason(index, cohort)[:2]

    def classify_with_reason(self, index, cohort):
        """Return destination, trace and one route outcome (or None outside its scope)."""
        values, tokens = self.values, self.tokens
        before = values[index - 1] if index else ""
        after = values[index + 1] if index + 1 < len(values) else ""
        if cohort == "colon_neighbor":
            if after == ":" and index + 1 in self.colon_after_key:
                return "object_key", "unbound", None
            if before == ":" and index - 1 in self.colon_after_key:
                obj = self.parent[index]
                key = _key(tokens[index - 2])
                destination = "object_value_" + _field_kind(key)
                if self.object_keys[obj, key] != 1 or obj in self.spread_frames:
                    return destination, "ambiguous_write", "duplicate_or_spread_property"
                owner = self.objects[obj]
                if owner is None:
                    end = self.close.get(obj)
                    target = self._argument_target(obj, end + 1) if end is not None else (None, "object_boundary_unknown")
                    trace, reason = self._function_trace((target,), key)
                    if reason == "no_direct_argument":
                        reason = "object_not_direct_argument"
                else:
                    trace, reason = self._trace(owner, key, index, own_write=0)
                return destination, trace or "unbound", reason
            if (before == ":" and index - 1 in self.ternary) or (after == ":" and index + 1 in self.ternary):
                return "conditional_arm", "unbound", None
            if before == "case" and after == ":":
                return "switch_case_label", "unbound", None
            return "unresolved_colon", "unbound", None
        if cohort != "equals_prefix" or before != "=" or index < 2:
            raise ValueError("not a supported colon or assignment cohort")
        if values[index - 2] in _ASSIGNMENT_PREFIX:
            # The SDK lexer emits individual punctuators, not combined operators.
            return "compound_or_comparison", "unbound", None
        if tokens[index - 2][0] == "name":
            name = values[index - 2]
            if index >= 4 and values[index - 3] == ".":
                receiver = values[index - 4]
                owner = (self.scope[index], receiver) if tokens[index - 4][0] == "name" and (
                    index < 5 or values[index - 5] not in {".", "?"}
                ) else None
                trace, _ = self._trace(owner, name, index, own_write=1)
                return "member_assignment_" + _field_kind(name), trace, None
            owner = (self.scope[index], name)
            destination = "local_initializer" if index >= 3 and values[index - 3] in _DECLARATIONS else "identifier_assignment"
            trace, _ = self._trace(owner, None, index, own_write=1)
            return destination, trace, None
        if values[index - 2] == "]":
            trace, reason = self._indexed_trace(index - 2, index)
            if (index >= 5 and values[index - 4] == "[" and tokens[index - 5][0] == "name"
                    and tokens[index - 3][0] == "string" and (key := _key(tokens[index - 3])) is not None
                    and (index < 6 or values[index - 6] not in {".", "?"})):
                owner = (self.scope[index], values[index - 5])
                direct, _ = self._trace(owner, key, index, own_write=1)
                if trace is None and direct not in {"ambiguous_write", "unbound"}:
                    direct = "no_direct_read"
                return "indexed_assignment_" + _field_kind(key), trace or direct, reason
            return "indexed_assignment", trace or "unbound", reason
        return "unresolved_assignment", "unbound", None
