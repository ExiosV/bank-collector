"""A fake browser page built on the standard library only.

The tests must not import Playwright — including Playwright's own selectors —
so this module implements the small part of the DOM that
``bank_collector.collect`` needs:

* ``goto(url)``
* ``wait_for_selector(selector, timeout=...)``
* ``query_all(selector)``
* ``content()`` / ``inner_text(selector)``

The CSS subset supported here covers what the shipped profiles use:
``tag``, ``.class``, ``#id`` and the descendant combinator (space).
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
        return [node for node in self.iter_descendants() if _matches_chain(node, selector)]

    def text_content(self):
        parts = list(self._text_parts)
        for child in self.children:
            parts.append(child.text_content())
        return "".join(parts)

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


_COMPOUND_RE = re.compile(
    r"(?P<tag>[a-zA-Z][\w-]*)?"
    r"(?P<rest>(?:[.#][\w-]+)*)$"
)


def _parse_compound(compound):
    match = _COMPOUND_RE.match(compound)
    if not match:
        return None
    tag = match.group("tag")
    classes = []
    element_id = None
    for token in re.findall(r"[.#][\w-]+", match.group("rest") or ""):
        if token.startswith("."):
            classes.append(token[1:])
        else:
            element_id = token[1:]
    return tag, classes, element_id


def _matches_compound(element, compound):
    parsed = _parse_compound(compound)
    if parsed is None:
        return False
    tag, classes, element_id = parsed
    if tag and element.tag.lower() != tag.lower():
        return False
    if element_id and element.attrs.get("id") != element_id:
        return False
    if classes:
        own = (element.attrs.get("class") or "").split()
        for wanted in classes:
            if wanted not in own:
                return False
    return True


def _matches_chain(element, selector):
    parts = [part for part in selector.strip().split() if part]
    if not parts:
        return False
    if not _matches_compound(element, parts[-1]):
        return False

    ancestor = element.parent
    for part in reversed(parts[:-1]):
        found = False
        while ancestor is not None:
            if _matches_compound(ancestor, part):
                found = True
                ancestor = ancestor.parent
                break
            ancestor = ancestor.parent
        if not found:
            return False
    return True


class FakePage(object):
    """A stand-in for a Playwright page, backed by a static HTML string."""

    def __init__(self, html, url="https://example.invalid/cabinet"):
        self.html = html
        self.root = parse_html(html)
        self.url = url
        self.visited = []
        self.waited_for = []

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

    def content(self):
        return self.html

    def inner_text(self, selector="body"):
        if selector in (None, "", "body"):
            return self.root.text_content()
        matches = self.root.query_all(selector)
        if not matches:
            return ""
        return matches[0].text_content()
