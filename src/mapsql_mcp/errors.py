"""Error types and secret redaction.

Anything that can reach an MCP client (tool errors, logs) goes through
:func:`redact` so that connection strings and passwords never leak.
"""

from __future__ import annotations

import re

_DSN_RE = re.compile(r"(?i)\b(?:postgres(?:ql)?|postgis|oracle(?:db)?)(?:\+\w+)?://[^\s'\"<>]+")
_EASY_CONNECT_RE = re.compile(r"(?i)\btcps?://[^\s'\"<>]+")
_KV_SECRET_RE = re.compile(r"(?i)\b(password|passwd|pwd|sslpassword|sslkey|passfile)\s*=\s*('[^']*'|\"[^\"]*\"|\S+)")
_HOST_RE = re.compile(r"(?i)\bhost\s*=\s*('[^']*'|\S+)")

_extra_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember a literal secret (e.g. the DB password) to scrub from all messages."""
    if value and len(value) >= 3:
        _extra_secrets.add(value)


def _mask_url(m: "re.Match[str]") -> str:
    url = m.group(0)
    if re.search(r"(?i)://user:pass@", url):  # documentation placeholder, not a secret: keep error hints readable
        return url
    return url.split("://", 1)[0] + "://***"


def _redact_descriptors(s: str) -> str:
    """Replace Oracle TNS connect descriptors ``(DESCRIPTION=...)`` (nested parentheses) with a placeholder."""
    low = s.lower()
    out: list[str] = []
    i = 0
    while True:
        j = low.find("(description", i)
        if j < 0:
            out.append(s[i:])
            return "".join(out)
        out.append(s[i:j])
        depth, k = 0, j
        while k < len(s):
            if s[k] == "(":
                depth += 1
            elif s[k] == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        out.append("(DESCRIPTION=***)")
        i = k


def redact(text: object) -> str:
    s = str(text)
    s = _DSN_RE.sub(_mask_url, s)
    s = _EASY_CONNECT_RE.sub("tcps://***", s)
    if "(" in s:
        s = _redact_descriptors(s)
    s = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", s)
    s = _HOST_RE.sub("host=***", s)
    for secret in _extra_secrets:
        s = s.replace(secret, "***")
    return s


class MapsqlError(Exception):
    """An expected, user-facing error. The message is safe to show to the model."""

    def __init__(self, message: str):
        super().__init__(redact(message))


class GuardError(MapsqlError):
    """Raised when SQL is rejected by the safety guard."""


class LayerNotFound(MapsqlError):
    pass


class NeedsClarification(MapsqlError):
    """The request is ambiguous or incomplete; the model should ask the user."""
