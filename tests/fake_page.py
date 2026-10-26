"""A fake browser page built on the standard library only.

The tests must not import Playwright — including Playwright's own selectors —
so this module implements the small part of the DOM that
``bank_collector.collect`` needs:

* ``goto(url)``
* ``wait_for_selector(selector, timeout=...)``
* ``query_all(selector)``
* ``content()`` / ``inner_text(selector)``
* ``scroll_by(dy)`` -- read-only: it only moves the viewport
* a ``click()`` hook: ``on_click(page, element)`` is called when a test
  clicks an element, so that a click can grow the DOM like an SPA does

It also ships a small CSS engine, because real bank pages are anchored on
``data-test-id`` attributes and nest their rows deeply. The subset supported
here covers what the shipped profiles use:

* ``tag``, ``.class``, ``#id``;
* the descendant combinator (a space) and the child combinator (``>``);
* attribute selectors: ``[a]``, ``[a=v]``, ``[a^=v]``, ``[a$=v]``,
  ``[a*=v]`` — the value may be quoted or not;
* ``:has(<inner>)``: the element contains a matching descendant
  (``div:has(span)``) or a matching direct child (``div:has(> span)``).
  Several ``:has()`` may sit in one compound selector and one may hold
  another one (``div:has(> [data-date]):has(> button)``).

Only the standard library is used.
"""

import re
from html.parser import HTMLParser

__all__ = ["FakePage", "FakeElement", "parse_html"]

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class FakeElement(object):
    """One element of the parsed tree."""

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.parent = parent
        self.children = []
        self._text_parts = []
        #: Everything a test wrote into this element, in order: ("fill", value)
        #: or ("click", None).  The real page has no such bookkeeping; it is
        #: here so that the unlock tests can prove that nothing else is tapped.
        self.writes = []

    # -- tree ------------------------------------------------------------
    def append_child(self, child):
        child.parent = self
        self.children.append(child)

    def add_text(self, text):
        self._text_parts.append(text)

    def iter_descendants(self):
        for child in self.children:
            yield child
            for grand in child.iter_descendants():
                yield grand

    # -- DOM API used by the collector -----------------------------------
    def query_all(self, selector):
        steps = _parse_selector(selector)
        return [node for node in self.iter_descendants() if _matches_steps(node, steps)]

    def text_content(self):
        parts = list(self._text_parts)
        for child in self.children:
            parts.append(child.text_content())
        return "".join(parts)

    # -- the write primitives (used by bank_collector.unlock and the one
    #    load-more click of the collector) -------------------------------
    def fill(self, value):
        self.writes.append(("fill", value))
        self.attrs["value"] = value
        return None

    def click(self):
        self.writes.append(("click", None))
        # A click may change the page: the root carries the page's hook.
        root = self
        while root.parent is not None:
            root = root.parent
        hook = getattr(root, "click_hook", None)
        if hook is not None:
            hook(self)
        return None

    def get_attribute(self, name):
        return self.attrs.get(name)


class _Builder(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = FakeElement("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        element = FakeElement(tag, dict(attrs))
        self.stack[-1].append_child(element)
        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        element = FakeElement(tag, dict(attrs))
        self.stack[-1].append_child(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].add_text(data)


def parse_html(html):
    """Parse *html* and return the root element."""
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


# ---------------------------------------------------------------------------
# A tiny CSS engine
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
_NAME_RE = re.compile(r"[^\s~^$*|=]+")
_ATTR_RE = re.compile(r"\s*([^\s~^$*|=]+)\s*(.*)$", re.S)
_OP_RE = re.compile(r"\s*(~=|\|=|\^=|\$=|\*=|=)\s*(.*)$", re.S)


class _Compound(object):
    """One compound selector: a tag plus classes, id, attributes and :has()."""

    __slots__ = ("tag", "classes", "ids", "attrs", "has")

    def __init__(self):
        self.tag = None
        self.classes = []
        self.ids = []
        self.attrs = []
        self.has = []


def _find_closing(text, start, opener, closer):
    """Index just after the closing *closer* that matches *opener* at *start*."""
    depth = 0
    quote = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _parse_attribute(text, start):
    """Parse ``[name op value]`` at *start*; return ``((name, op, value), end)``."""
    end = _find_closing(text, start, "[", "]")
    body = text[start + 1 : end - 1 if text[end - 1 : end] == "]" else end]

    match = _ATTR_RE.match(body)
    if not match:
        return ("", None, None), end
    name = match.group(1)
    rest = match.group(2)
    if not rest.strip():
        return (name, None, None), end

    op_match = _OP_RE.match(rest)
    if not op_match:
        return (name, None, None), end
    operator = op_match.group(1)
    value = op_match.group(2).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return (name, operator, value), end


def _parse_compound(text):
    """Parse a compound selector (no combinators inside)."""
    compound = _Compound()
    index = 0
    length = len(text)

    if index < length and text[index].isalpha():
        tag_match = _TAG_RE.match(text, index)
        compound.tag = tag_match.group(0)
        index = tag_match.end()

    while index < length:
        char = text[index]
        if char == ".":
            name_match = _NAME_RE.match(text, index + 1)
            if not name_match:
                break
            compound.classes.append(name_match.group(0))
            index = name_match.end()
        elif char == "#":
            name_match = _NAME_RE.match(text, index + 1)
            if not name_match:
                break
            compound.ids.append(name_match.group(0))
            index = name_match.end()
        elif char == "[":
            attribute, index = _parse_attribute(text, index)
            compound.attrs.append(attribute)
        elif char == ":":
            name_match = _TAG_RE.match(text, index + 1)
            if not name_match:
                break
            name = name_match.group(0).lower()
            index = name_match.end()
            if name == "has":
                after = _find_closing(text, index, "(", ")")
                inner = text[index + 1 : after - 1 if text[after - 1 : after] == ")" else after]
                compound.has.append(_parse_steps(inner))
                index = after
            else:
                # Unknown pseudo-classes are ignored, like a browser would
                # ignore an unknown one it cannot match.
                pass
        else:
            index += 1

    return compound


def _parse_steps(text):
    """Parse a selector into a list of ``(relation, compound)`` steps.

    *relation* says how the compound relates to the previous one: ``' '``
    (descendant) or ``'>'`` (direct child). For the first step the relation
    says how it relates to the scope of the selector (``':has(> x)'`` uses a
    direct child of the scoped element).
    """
    steps = []
    index = 0
    length = len(text)
    pending = None

    while index < length:
        start = index
        while index < length and text[index].isspace():
            index += 1
        saw_space = index > start

        if index >= length:
            break
        if text[index] == ">":
            pending = ">"
            index += 1
            continue
        if saw_space and steps and pending is None:
            pending = " "

        parts = []
        while index < length:
            char = text[index]
            if char == "(":
                after = _find_closing(text, index, "(", ")")
                parts.append(text[index:after])
                index = after
                continue
            if char.isspace() or char == ">":
                break
            parts.append(char)
            index += 1

        chunk = "".join(parts)
        if not chunk:
            break
        steps.append((pending if pending is not None else " ", _parse_compound(chunk)))
        pending = None

    return steps


_SELECTOR_CACHE = {}


def _parse_selector(selector):
    """Parse (and cache) *selector* into steps."""
    steps = _SELECTOR_CACHE.get(selector)
    if steps is None:
        steps = _parse_steps(selector)
        _SELECTOR_CACHE[selector] = steps
    return steps


def _matches_compound(element, compound):
    """True when *element* matches the simple selectors of *compound*."""
    if compound.tag is not None and element.tag.lower() != compound.tag.lower():
        return False

    if compound.classes:
        own = (element.attrs.get("class") or "").split()
        for wanted in compound.classes:
            if wanted not in own:
                return False

    if compound.ids and element.attrs.get("id") not in compound.ids:
        return False

    for name, operator, value in compound.attrs:
        actual = element.attrs.get(name)
        if actual is None:
            return False
        if operator is None:
            continue
        if operator == "=":
            if actual != value:
                return False
        elif operator == "^=":
            if not actual.startswith(value):
                return False
        elif operator == "$=":
            if not actual.endswith(value):
                return False
        elif operator == "*=":
            if value not in actual:
                return False
        elif operator == "~=":
            if value not in actual.split():
                return False
        elif operator == "|=":
            if not (actual == value or actual.startswith(value + "-")):
                return False

    for inner in compound.has:
        if not _matches_relative(element, inner):
            return False

    return True


def _matches_relative(scope, steps):
    """True when *scope* contains an element matching the relative *steps*."""
    if not steps:
        return False

    relation = steps[0][0]
    if relation == ">":
        roots = list(scope.children)
    else:
        roots = list(scope.iter_descendants())

    for root in roots:
        if _matches_compound(root, steps[0][1]) and _matches_descendants(root, steps, 1):
            return True
    return False


def _matches_descendants(node, steps, index):
    """True when the chain below *node* matches ``steps[index:]``."""
    if index >= len(steps):
        return True

    relation, compound = steps[index]
    if relation == ">":
        pool = list(node.children)
    else:
        pool = list(node.iter_descendants())

    for candidate in pool:
        if _matches_compound(candidate, compound) and _matches_descendants(candidate, steps, index + 1):
            return True
    return False


def _matches_ancestors(node, steps, index):
    """True when the ancestors of *node* match ``steps[:index]``."""
    if index <= 0:
        return True

    relation = steps[index][0]
    previous = steps[index - 1][1]

    if relation == ">":
        parent = node.parent
        if parent is None or not _matches_compound(parent, previous):
            return False
        return _matches_ancestors(parent, steps, index - 1)

    ancestor = node.parent
    while ancestor is not None:
        if _matches_compound(ancestor, previous) and _matches_ancestors(ancestor, steps, index - 1):
            return True
        ancestor = ancestor.parent
    return False


def _matches_steps(element, steps):
    """True when *element* matches the whole (absolute) selector."""
    if not steps:
        return False
    if not _matches_compound(element, steps[-1][1]):
        return False
    return _matches_ancestors(element, steps, len(steps) - 1)


class FakePage(object):
    """A stand-in for a Playwright page, backed by a static HTML string."""

    def __init__(
        self, html, url="https://example.invalid/cabinet", pages=None, on_scroll=None, on_click=None
    ):
        self.url = url
        self.visited = []
        self.waited_for = []
        #: Every scroll the driver asked for, in order: ("scroll", dy).
        #: Scrolling is not a write, so it has its own log (see ``writes``).
        self.scrolls = []
        self._pages = list(pages or [])
        self._on_scroll = on_scroll
        self._on_click = on_click
        self._set_root(html)

    def _set_root(self, html):
        """Parse *html* and let every click on the new tree reach the hook."""
        self.html = html
        self.root = parse_html(html)
        self.root.click_hook = self._dispatch_click

    def _dispatch_click(self, element):
        if self._on_click is not None:
            self._on_click(self, element)

    # -- playwright-like API ---------------------------------------------
    def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = url
        return None

    def wait_for_selector(self, selector, timeout=None):
        self.waited_for.append(selector)
        if not self.root.query_all(selector):
            raise LookupError("selector %r not found" % selector)
        return True

    def query_all(self, selector):
        return self.root.query_all(selector)

    def scroll_by(self, dy):
        """Pretend to scroll the viewport by *dy* pixels.

        Scrolling is read-only: nothing is written into the DOM, only the
        ``scrolls`` log grows.  A page built with ``pages`` serves the next
        HTML document on every scroll (that is how an SPA appends rows), a
        page built with ``on_scroll`` lets the test grow the DOM itself.
        """
        self.scrolls.append(("scroll", dy))
        if self._pages:
            self._set_root(self._pages.pop(0))
        if self._on_scroll is not None:
            self._on_scroll(self)
        return None

    @property
    def writes(self):
        """Every write action recorded on the current document, in order."""
        collected = list(getattr(self.root, "writes", []))
        for element in self.root.iter_descendants():
            collected.extend(getattr(element, "writes", []))
        return collected

    def content(self):
        return self.html

    def inner_text(self, selector="body"):
        if selector in (None, "", "body"):
            return self.root.text_content()
        matches = self.root.query_all(selector)
        if not matches:
            return ""
        return matches[0].text_content()
