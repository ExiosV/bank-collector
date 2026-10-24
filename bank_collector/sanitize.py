"""Removing personal data from a snapshot before it is written to disk.

A snapshot is meant to help you write CSS selectors, so it keeps the DOM
structure but drops everything that could carry personal data:

* ``<script>`` / ``<style>`` bodies are removed;
* ``<input>`` / ``<textarea>`` values are removed (they may hold a masked
  card number or a session token);
* long digit runs (card numbers, phone numbers, account numbers) are
  replaced with ``*``;
* e-mail addresses and any value that looks like a credential is redacted.

The result is still very useful for selector work and far less risky to
share in a bug report.
"""

import re

__all__ = ["sanitize_html", "sanitize_text"]

_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_INPUT_VALUE_RE = re.compile(r"(<(?:input|textarea)\b[^>]*?)\svalue=([\"']).*?\2", re.IGNORECASE | re.DOTALL)
_INPUT_TAG_RE = re.compile(r"<(input|textarea)\b[^>]*>", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LONG_DIGITS_RE = re.compile(r"\d{6,}")
_PASSWORDISH_RE = re.compile(
    r"(?i)\b(pass(word)?|secret|token|otp|passcode|cvc|cvv)\b\s*[:=]?\s*[\"']?([^\s\"',;<>]{3,})"
)
_URL_CREDENTIALS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+):([^\s@]+)@")


def _redact_digits(match):
    digits = match.group(0)
    return "*" * len(digits)


def _redact_passwordish(match):
    return "%s: ***" % match.group(1)


def _redact_url_credentials(match):
    return "%s:***@%s" % (match.group(1), match.group(3))


def sanitize_html(html):
    """Return a copy of *html* without scripts, values and personal data."""
    if not html:
        return ""
    text = _COMMENT_RE.sub("", html)
    text = _SCRIPT_RE.sub("", text)
    text = _INPUT_VALUE_RE.sub(r"\1 value=\"\"", text)
    text = _INPUT_TAG_RE.sub(r"<\1 type=\"redacted\">", text)
    text = _URL_CREDENTIALS_RE.sub(_redact_url_credentials, text)
    text = _PASSWORDISH_RE.sub(_redact_passwordish, text)
    text = _EMAIL_RE.sub("***@***", text)
    text = _LONG_DIGITS_RE.sub(_redact_digits, text)
    return text


def sanitize_text(text):
    """Return a copy of plain *text* without e-mails and long digit runs."""
    if not text:
        return ""
    text = _URL_CREDENTIALS_RE.sub(_redact_url_credentials, text)
    text = _PASSWORDISH_RE.sub(_redact_passwordish, text)
    text = _EMAIL_RE.sub("***@***", text)
    text = _LONG_DIGITS_RE.sub(_redact_digits, text)
    return text
