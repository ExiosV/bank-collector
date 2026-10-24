"""Minimal YAML subset parser (stdlib only).

`bank-collector` declares exactly one runtime dependency: `playwright`.
Bank profiles are small, well-known YAML documents, so instead of adding
PyYAML we ship a tiny parser that understands the subset we actually use:

  * block mappings (``key: value``)
  * block sequences (``- item``)
  * nested blocks, scalars (str/int/float/bool/null)
  * single/double quoted scalars, inline comments

If PyYAML happens to be installed it is used instead (see ``bank_collector.profiles``);
the two parsers are checked against each other in the test-suite.
"""

import re

__all__ = ["safe_load", "YamlError"]


class YamlError(ValueError):
    """Raised when the document is not in the supported YAML subset."""


_KEY_RE = re.compile(r"^([^:#]+?)\s*:(?:\s+(.*))?$")
_INT_RE = re.compile(r"^[-+]?\d+$")
_FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?$")


def safe_load(text):
    """Parse *text* and return plain Python data."""
    lines = _tokenize(text)
    if not lines:
        return {}
    parser = _Parser(lines)
    value = parser.parse_block(lines[0][0])
    if parser.index < len(lines):
        raise YamlError("unexpected content at line %d" % (parser.index + 1))
    return value


def _strip_comment(raw):
    out = []
    quote = None
    prev = ""
    for ch in raw:
        if quote:
            out.append(ch)
            if ch == quote and prev != "\\":
                quote = None
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "#" and (not out or out[-1] in " \t"):
            break
        else:
            out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def _tokenize(text):
    tokens = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.strip().startswith("#"):
            continue
        without = _strip_comment(raw)
        if not without.strip():
            continue
        if "\t" in without[: len(without) - len(without.lstrip())]:
            raise YamlError("tabs are not allowed for indentation (line %d)" % number)
        indent = len(without) - len(without.lstrip(" "))
        tokens.append((indent, without.strip(), number))
    return tokens


def _split_key(text):
    match = _KEY_RE.match(text)
    if not match:
        return None, None
    return match.group(1).strip().strip("'\""), match.group(2)


class _Parser(object):
    def __init__(self, tokens):
        self.tokens = tokens
        self.index = 0

    # -- helpers ---------------------------------------------------------
    def _peek(self):
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    # -- public ----------------------------------------------------------
    def parse_block(self, indent):
        token = self._peek()
        if token is None:
            return None
        if token[1] == "-" or token[1].startswith("- "):
            return self.parse_sequence(indent)
        return self.parse_mapping(indent)

    def parse_mapping(self, indent):
        result = {}
        while True:
            token = self._peek()
            if token is None or token[0] < indent:
                return result
            if token[0] > indent:
                raise YamlError("bad indentation at line %d" % token[2])
            if token[1].startswith("- ") or token[1] == "-":
                return result
            key, value = _split_key(token[1])
            if key is None:
                raise YamlError("expected 'key: value' at line %d" % token[2])
            self.index += 1
            if value is None or value == "":
                nxt = self._peek()
                if nxt is not None and nxt[0] > indent:
                    result[key] = self.parse_block(nxt[0])
                else:
                    result[key] = None
            else:
                result[key] = _scalar(value, token[2])
        return result

    def parse_sequence(self, indent):
        items = []
        while True:
            token = self._peek()
            if token is None or token[0] != indent:
                return items
            if not (token[1] == "-" or token[1].startswith("- ")):
                return items
            content = token[1][1:].lstrip()
            inline_indent = indent + (len(token[1]) - len(token[1].lstrip("- ")))
            if not content:
                self.index += 1
                nxt = self._peek()
                if nxt is not None and nxt[0] > indent:
                    items.append(self.parse_block(nxt[0]))
                else:
                    items.append(None)
                continue
            key, _ = _split_key(content)
            if key is not None:
                # "- key: value" starts a mapping whose indent is the column of "key".
                # Rewrite the dash line in place and let parse_mapping consume it.
                self.tokens[self.index] = (inline_indent, content, token[2])
                items.append(self.parse_mapping(inline_indent))
            else:
                self.index += 1
                items.append(_scalar(content, token[2]))
        return items


def _scalar(text, line=0):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    low = text.lower()
    if low in ("null", "~", ""):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text
