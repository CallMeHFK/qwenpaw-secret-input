# -*- coding: utf-8 -*-
"""Egress scrubber: replace known plaintext (and encodings) in tool output.

Best-effort by design (spec section 10.2). The host's masking helper is
deliberately not used, not even mentioned in this module: it keeps the leading
2-3 and trailing 2-4 characters of a value, which would turn a zero-char leak
into a several-char leak (spec section 10 hard prohibition, verified against
the host source). The replacement marker is always the full REDACT constant.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections import OrderedDict
from urllib.parse import quote

logger = logging.getLogger(__name__)

REDACT = "***"
_MIN_VALUE_CHARS = 8


class Scrubber:
    def __init__(self, *, ttl_seconds: int = 3600, max_entries: int = 64) -> None:
        self._ttl = max(int(ttl_seconds), 1)
        self._max = max(int(max_entries), 1)
        self._entries: "OrderedDict[str, float]" = OrderedDict()

    def variants(self, value: str) -> list[str]:
        # Five variant families (spec section 3 I4): raw / base64 / base64url /
        # hex / percent. Hex is tried in both cases because a hex dump is as
        # likely to come out upper case as lower. The dedup drops a form that
        # equals one already in the list (a value made only of unreserved
        # characters percent-encodes to itself; a value whose base64 has no
        # "+" or "/" is urlsafe-equal), so no form is scanned twice in a pass.
        raw = value.encode("utf-8")
        out = [value]
        for form in (
            base64.b64encode(raw).decode("ascii"),
            base64.urlsafe_b64encode(raw).decode("ascii"),
            binascii.hexlify(raw).decode("ascii"),
            binascii.hexlify(raw).decode("ascii").upper(),
            quote(value, safe=""),
        ):
            if form not in out:
                out.append(form)
        return out

    def register(self, value: str) -> None:
        # Refusing is a log line, never an exception: a caller that hands us a
        # non-string or a too-short value must keep working. The value itself
        # never goes into the message (spec section 10: no plaintext in logs).
        if not isinstance(value, str) or len(value) < _MIN_VALUE_CHARS:
            logger.info("secret-input: scrub skipped for a value under %d chars",
                        _MIN_VALUE_CHARS)
            return
        self._entries[value] = time.monotonic()
        # OrderedDict order must equal "least recently registered" order, so
        # popitem(last=False) below can never evict a value that was just
        # re-registered (Task 8 re-registers on every consumption).
        self._entries.move_to_end(value)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def _live_values(self) -> list[str]:
        now = time.monotonic()
        # Longest first so an encoded form cannot be partially eaten by a
        # shorter neighbour's replacement.
        return sorted(
            (v for v, at in self._entries.items() if now - at <= self._ttl),
            key=len,
            reverse=True,
        )

    def replace(self, text: str) -> str:
        # Total: an empty or non-string input comes back untouched, so a weird
        # block can never make the caller's scrubbing step raise. With nothing
        # registered the function is the identity (Task 5 relies on that).
        if not isinstance(text, str) or not text:
            return text
        out = text
        for value in self._live_values():
            for form in self.variants(value):
                if form in out:
                    out = out.replace(form, REDACT)
        return out

    def sweep(self) -> int:
        now = time.monotonic()
        stale = [v for v, at in self._entries.items() if now - at > self._ttl]
        for value in stale:
            self._entries.pop(value, None)
        return len(stale)

    def __contains__(self, value: str) -> bool:
        return value in self._entries

    def __len__(self) -> int:
        return len(self._entries)
