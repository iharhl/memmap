"""Read a devicetree source file, in the subset that Zephyr's merged `zephyr.dts` uses.

The build writes `build/zephyr/zephyr.dts` after it has run the C preprocessor and merged the
board files and overlays, so the file has no `#include`, macros or expressions: nodes with
labels, and properties holding cell lists, strings, byte strings and references. That is what
this reads. A source `.dts` or overlay still needs the preprocessor, so it is refused with a
hint to use the build's file instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..from_elf.regions import ImportProblem

MERGED_HINT = "pass the merged build/zephyr/zephyr.dts that the build writes"

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
    | (?P<comment>/\*.*?\*/|//[^\n]*)
    | (?P<string>"(?:[^"\\\n]|\\.)*")
    | (?P<char>'(?:[^'\\\n]|\\.)+')
    | (?P<directive>/[a-z][a-z0-9-]*/)
    | (?P<ref>&\{[^}]*\}|&[A-Za-z_][A-Za-z0-9_]*)
    | (?P<label>[A-Za-z_][A-Za-z0-9_]*:(?!:))
    | (?P<preprocessor>\#(?:include|define|undef|ifdef|ifndef|if|elif|else|endif)\b(?![-,])
        | ^\#\s*\d+\s+")
    | (?P<name>[A-Za-z0-9,._+*\#?@-]+|/)
    | (?P<punct>[{};=<>\[\],()])
    """,
    re.VERBOSE | re.DOTALL | re.MULTILINE,
)
_NUMBER = re.compile(r"(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)(?:[uU]?[lL]{0,2}|[lL]{1,2}[uU]?)")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'", "a": "\a"}


@dataclass(frozen=True)
class Token:
    """One token: its kind (a group name of _TOKEN), its text and the line it starts on."""

    kind: str
    text: str
    line: int


@dataclass(frozen=True)
class Reference:
    """A reference to a node, `&label` or `&{/path}`, inside a cell list or as a value."""

    target: str


@dataclass
class Property:
    """A property: its values in order. A cell list is a list of ints and References, a string
    a str, a byte string bytes and a reference a Reference. A property without a value, such
    as `read-only;`, has none."""

    name: str
    values: list[list[int | Reference] | str | bytes | Reference] = field(default_factory=list)
    line: int = 0

    def cells(self) -> list[int]:
        """The numbers of all its cell lists, in order. Raises ValueError for anything else."""
        numbers: list[int] = []
        for value in self.values:
            if not isinstance(value, list):
                raise ValueError(f"{self.name} is not a cell list")
            for cell in value:
                if not isinstance(cell, int):
                    raise ValueError(f"{self.name} holds a reference, not a number")
                numbers.append(cell)
        return numbers

    def strings(self) -> list[str]:
        """Its strings, in order. Raises ValueError when a value is not a string."""
        if not all(isinstance(value, str) for value in self.values):
            raise ValueError(f"{self.name} is not a string")
        return [value for value in self.values if isinstance(value, str)]


@dataclass
class Node:
    """A node: its name with the unit address, the labels in front of it, its properties and
    its children in file order. A node defined twice is merged, as dtc does."""

    name: str
    labels: list[str] = field(default_factory=list)
    properties: dict[str, Property] = field(default_factory=dict)
    children: list[Node] = field(default_factory=list)
    parent: Node | None = None
    line: int = 0

    @property
    def path(self) -> str:
        """The full path, such as /soc/flash-controller@4001e000/flash@0."""
        if self.parent is None:
            return "/"
        parent = self.parent.path
        return f"{parent}{'' if parent.endswith('/') else '/'}{self.name}"

    @property
    def unit_address(self) -> str | None:
        """The part of the name after @, or None."""
        return self.name.split("@", 1)[1] if "@" in self.name else None

    def child(self, name: str) -> Node | None:
        """The child with this name, or None."""
        return next((child for child in self.children if child.name == name), None)

    def walk(self) -> list[Node]:
        """This node and every node below it, depth first, in file order."""
        nodes = [self]
        for child in self.children:
            nodes += child.walk()
        return nodes

    def compatible(self) -> list[str]:
        """The `compatible` strings, or none."""
        found = self.properties.get("compatible")
        try:
            return found.strings() if found else []
        except ValueError:
            return []

    def cell_count(self, name: str, default: int) -> int:
        """`#address-cells` or `#size-cells` as this node sets it for its children."""
        found = self.properties.get(name)
        if found is None:
            return default
        try:
            numbers = found.cells()
        except ValueError:
            return default
        return numbers[0] if len(numbers) == 1 else default


def _tokens(text: str) -> list[Token]:
    """The tokens of `text`, without spaces and comments."""
    tokens: list[Token] = []
    line, at = 1, 0
    while at < len(text):
        match = _TOKEN.match(text, at)
        if match is None:
            raise ImportProblem(f"line {line}: unexpected {text[at]!r}", hint=MERGED_HINT)
        kind = match.lastgroup
        assert kind is not None
        chunk = match.group()
        if kind == "preprocessor":
            what = (
                "a line marker of the C preprocessor, as in zephyr.dts.pre"
                if chunk[1:].lstrip()[:1].isdigit()
                else f"{chunk}, a preprocessor directive"
            )
            raise ImportProblem(f"line {line}: {what}, which zephyr.dts does not have", hint=MERGED_HINT)
        if kind not in ("space", "comment"):
            tokens.append(Token(kind, chunk, line))
        line += chunk.count("\n")
        at = match.end()
    return tokens


def _number(text: str) -> int | None:
    """A C integer literal, such as 0x1000, 017 or 42UL, or None when it is not one."""
    match = _NUMBER.fullmatch(text)
    if match is None:
        return None
    digits = match.group(1)
    if digits[:2].lower() == "0x":
        return int(digits, 16)
    return int(digits, 8) if digits.startswith("0") else int(digits)


def _unescape(body: str) -> str:
    """A string or char literal's text with its escapes resolved."""

    def replace(match: re.Match[str]) -> str:
        """One escape: a named one, \\x hex or octal digits."""
        escape = match.group(1)
        if escape[0] == "x":
            return chr(int(escape[1:], 16))
        if escape[0] in "01234567" and escape != "0":
            return chr(int(escape, 8))
        return _ESCAPES.get(escape, escape)

    return re.sub(r"\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)", replace, body)


class _Parser:
    """A recursive descent parser over the token list."""

    def __init__(self, tokens: list[Token]) -> None:
        """Start at the first token."""
        self.tokens = tokens
        self.at = 0
        self.root = Node("/")

    def peek(self) -> Token | None:
        """The next token, or None at the end."""
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> Token:
        """The next token, which must exist."""
        token = self.peek()
        if token is None:
            last = self.tokens[-1].line if self.tokens else 1
            raise ImportProblem(f"line {last}: the file ends inside a node", hint=MERGED_HINT)
        self.at += 1
        return token

    def expect(self, text: str) -> Token:
        """The next token, which must be `text`."""
        token = self.take()
        if token.text != text:
            raise ImportProblem(f"line {token.line}: expected {text!r}, found {token.text!r}")
        return token

    def labels(self) -> list[str]:
        """Labels in front of a node, property or value."""
        found = []
        while (token := self.peek()) is not None and token.kind == "label":
            found.append(self.take().text[:-1])
        return found

    def document(self) -> Node:
        """The whole file: directives, then top-level nodes."""
        while (token := self.peek()) is not None:
            if token.kind == "directive":
                self.directive()
                continue
            labels = self.labels()
            token = self.take()
            if token.text == "/":
                self.body(self.root, labels)
            elif token.kind == "ref":
                target = self.find(token)
                self.body(target, labels)
            else:
                raise ImportProblem(f"line {token.line}: expected a node, found {token.text!r}")
        if not self.tokens:
            raise ImportProblem("the file is empty", hint=MERGED_HINT)
        return self.root

    def directive(self) -> None:
        """A top-level directive: /dts-v1/, /plugin/, /memreserve/ or /delete-node/."""
        token = self.take()
        if token.text == "/memreserve/":
            self.take()
            self.take()
        elif token.text == "/delete-node/":
            ref = self.take()
            target = self.find(ref) if ref.kind == "ref" else None
            if target is not None and target.parent is not None:
                target.parent.children.remove(target)
        elif token.text not in ("/dts-v1/", "/plugin/"):
            raise ImportProblem(f"line {token.line}: unexpected {token.text}")
        self.expect(";")

    def find(self, token: Token) -> Node:
        """The node a top-level `&label` or `&{/path}` names."""
        target = token.text[1:]
        if target.startswith("{"):
            node: Node | None = self.root
            for part in target[1:-1].strip("/").split("/"):
                if part and node is not None:
                    node = node.child(part)
        else:
            node = next((n for n in self.root.walk() if target in n.labels), None)
        if node is None:
            raise ImportProblem(f"line {token.line}: {token.text} names no node")
        return node

    def body(self, node: Node, labels: list[str]) -> None:
        """A node's braces and what is in them, merged into `node`."""
        node.labels += [label for label in labels if label not in node.labels]
        self.expect("{")
        while True:
            token = self.peek()
            if token is not None and token.text == "}":
                self.take()
                self.expect(";")
                return
            if token is not None and token.kind == "directive":
                self.inner_directive(node)
                continue
            inner = self.labels()
            name = self.take()
            if name.kind != "name":
                raise ImportProblem(f"line {name.line}: expected a name, found {name.text!r}")
            following = self.peek()
            if following is not None and following.text == "{":
                child = node.child(name.text)
                if child is None:
                    child = Node(name.text, parent=node, line=name.line)
                    node.children.append(child)
                self.body(child, inner)
            else:
                node.properties[name.text] = self.property(name)

    def inner_directive(self, node: Node) -> None:
        """/delete-node/ name; or /delete-property/ name; inside a node."""
        token = self.take()
        name = self.take().text
        if token.text == "/delete-node/":
            node.children = [child for child in node.children if child.name != name]
        elif token.text == "/delete-property/":
            node.properties.pop(name, None)
        else:
            raise ImportProblem(f"line {token.line}: unexpected {token.text}")
        self.expect(";")

    def property(self, name: Token) -> Property:
        """A property after its name: `;`, or `=` and comma-separated values, then `;`."""
        found = Property(name.text, line=name.line)
        token = self.take()
        if token.text == ";":
            return found
        if token.text != "=":
            raise ImportProblem(f"line {token.line}: expected '=' or ';' after {name.text}")
        while True:
            self.labels()
            found.values.append(self.value())
            self.labels()
            token = self.take()
            if token.text == ";":
                return found
            if token.text != ",":
                raise ImportProblem(f"line {token.line}: expected ',' or ';', found {token.text!r}")

    def value(self) -> list[int | Reference] | str | bytes | Reference:
        """One value: a string, a cell list, a byte string or a reference."""
        token = self.take()
        if token.kind == "string":
            return _unescape(token.text[1:-1])
        if token.kind == "ref":
            return Reference(token.text[1:])
        if token.text == "/bits/":
            bits = self.take()
            if bits.text not in ("8", "16", "32", "64"):
                raise ImportProblem(f"line {bits.line}: /bits/ {bits.text} is not 8, 16, 32 or 64")
            token = self.take()
        if token.text == "<":
            return self.cells()
        if token.text == "[":
            return self.byte_string()
        raise ImportProblem(f"line {token.line}: expected a value, found {token.text!r}")

    def cells(self) -> list[int | Reference]:
        """A cell list after `<`, up to `>`."""
        cells: list[int | Reference] = []
        while True:
            self.labels()
            token = self.take()
            if token.text == ">":
                return cells
            if token.kind == "ref":
                cells.append(Reference(token.text[1:]))
            elif token.kind == "char":
                cells.append(ord(_unescape(token.text[1:-1])[0]))
            elif token.kind == "name" and (number := _number(token.text)) is not None:
                cells.append(number)
            elif token.text == "(":
                raise ImportProblem(
                    f"line {token.line}: an expression in a cell list, which the build's "
                    "zephyr.dts has already worked out",
                    hint=MERGED_HINT,
                )
            else:
                hint = MERGED_HINT if token.kind == "name" and token.text.isupper() else None
                raise ImportProblem(f"line {token.line}: {token.text!r} is not a cell", hint=hint)

    def byte_string(self) -> bytes:
        """A byte string after `[`, up to `]`: hex digits, two to a byte."""
        digits = ""
        while True:
            self.labels()
            token = self.take()
            if token.text == "]":
                break
            digits += token.text
        if len(digits) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", digits):
            raise ImportProblem(f"line {token.line}: bad byte string [{digits}]")
        return bytes.fromhex(digits)


def is_dts(text: str) -> bool:
    """Whether `text` is devicetree source: it starts with /dts-v1/; after comments."""
    stripped = re.sub(r"/\*.*?\*/|//[^\n]*", "", text[:4096], flags=re.DOTALL).lstrip()
    return stripped.startswith("/dts-v1/")


def looks_like_source(text: str) -> bool:
    """Whether `text` looks like devicetree source the preprocessor has not run on: an overlay
    or board file, with #include lines, a root node or a node named by its label."""
    return bool(re.search(r"^\s*(#include\b|/\s*\{|&[A-Za-z_]\w*\s*\{)", text, re.MULTILINE))


def parse(text: str) -> Node:
    """The root node of a devicetree source file."""
    return _Parser(_tokens(text)).document()
