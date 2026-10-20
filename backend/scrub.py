"""Egress scrubber: replace known plaintext (and encodings) in tool output.

Best-effort by design (spec section 10.2). The host's masking helper is
deliberately not used, not even mentioned in this module: it keeps the leading
2-3 and trailing 2-4 characters of a value, which would turn a zero-char leak
into a several-char leak (spec section 10 hard prohibition, verified against
the host source). The replacement marker is always the full REDACT constant.

Two structural promises the callers (Task 5 middleware, Task 8 tools) rely on:

* ``replace()`` is total. No registered value -- however hostile its characters
  (a lone surrogate from a JSON body is the real case) -- can make it raise or
  can disable the pass for the other values. A broken value loses its own
  variants for one pass; it never loses the pass.
* The registry view is live-only. ``len()`` and ``in`` agree with what
  ``replace()`` actually protects, because expiry is applied on read, so no
  periodic sweep is needed to keep ``scrub_entries`` honest or to hand
  TTL-expired plaintext back to the garbage collector.
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
        self._entries: OrderedDict[str, float] = OrderedDict()

    def variants(self, value: str) -> list[str]:
        # Five variant families (spec section 3 I4): raw / base64 / base64url /
        # hex / percent. Hex is tried in both cases because a hex dump is as
        # likely to come out upper case as lower. The dedup drops a form that
        # equals one already in the list (a value made only of unreserved
        # characters percent-encodes to itself; a value whose base64 has no
        # "+" or "/" is urlsafe-equal), so no form is scanned twice in a pass.
        # Both encoders run in "surrogatepass": a lone surrogate really does
        # reach us (json.loads decodes the escape "\ud800" in a card POST body
        # into an unpaired surrogate character, and spec section 10.2 item 6
        # says the local agent may POST /answer itself), while a strict encode
        # would raise on every later replace() call — permanently disabling I4
        # for the whole set, with the offending character and its offset in the
        # traceback. Surrogatepass keeps the value registered: stability is not
        # bought by dropping protection.
        raw = value.encode("utf-8", "surrogatepass")
        out = [value]
        for form in (
            base64.b64encode(raw).decode("ascii"),
            base64.urlsafe_b64encode(raw).decode("ascii"),
            binascii.hexlify(raw).decode("ascii"),
            binascii.hexlify(raw).decode("ascii").upper(),
            quote(value, safe="", errors="surrogatepass"),
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
        # Snapshot before touching anything: register() mutates _entries
        # (move_to_end / popitem) and this method prunes as it computes, so an
        # iterator over the live view could hit "dictionary changed size during
        # iteration". A copy makes that impossible without threads (and this
        # module must stay thread-free).
        entries = list(self._entries.items())
        now = time.monotonic()
        live: list[str] = []
        expired: list[str] = []
        for value, at in entries:
            (live if now - at <= self._ttl else expired).append(value)
        # Expiry is opportunistic on read. The plan has no periodic scrub sweep
        # (only the startup hook and uninstall), so without this the registry
        # would keep TTL-expired plaintexts in RAM forever — undercutting
        # "TTL only shrinks the window" (spec section 10.2 item 3) — and
        # __len__/__contains__ would report entries that replace() no longer
        # protects, which status() publishes as scrub_entries.
        for value in expired:
            self._entries.pop(value, None)
        live.sort(key=len, reverse=True)
        return live

    def _candidate_forms(self) -> list[str]:
        # Flatten every live value's forms into one deduplicated list, longest
        # form first. The sort axis must be the form's own length, not the
        # length of the raw value it came from: the hazard lives at form level.
        # A longer value's raw can be the middle slice of a shorter value's
        # base64 — replacing the raw first bites through the neighbour's base64,
        # and the leftover characters match nothing else, so plaintext stays in
        # the output. With equal-length raws the old order silently degrades to
        # registration order. Sorting forms by length removes both; ties cannot
        # be substrings of each other (equal length + containment means equal,
        # and equal strings are deduped). The intra-value order (hex 2n >
        # base64 ~1.33n > raw n) is now fixed by the same rule instead of
        # relying on the order the tuple is written in.
        seen: dict[str, None] = {}
        for value in self._live_values():
            try:
                forms = self.variants(value)
            except Exception:
                # Backstop: a future variant family that raises on some value
                # loses that one value for this pass, never the whole pass.
                # Nothing identifying is logged — not the value, not the
                # exception text (spec section 10.1: log and exception
                # channels).
                logger.info("secret-input: scrub lost one value's variants this pass")
                continue
            for form in forms:
                seen.setdefault(form, None)
        return sorted(seen, key=len, reverse=True)

    def replace(self, text: str) -> str:
        # Total: an empty or non-string input comes back untouched, so a weird
        # block can never make the caller's scrubbing step raise. With nothing
        # registered the function is the identity (Task 5 relies on that).
        if not isinstance(text, str) or not text:
            return text
        out = text
        for form in self._candidate_forms():
            try:
                if form in out:
                    out = out.replace(form, REDACT)
            except Exception:
                # Same backstop on the replacement step itself: skip one form,
                # never abort the pass, never let a message with a value in it
                # escape.
                logger.info("secret-input: scrub skipped one candidate form this pass")
                continue
        return out

    def sweep(self) -> int:
        # Snapshot first (same reason as _live_values): the pops below mutate
        # the dict while this pass is still reading it.
        now = time.monotonic()
        stale = [v for v, at in list(self._entries.items()) if now - at > self._ttl]
        for value in stale:
            self._entries.pop(value, None)
        return len(stale)

    def __contains__(self, value: str) -> bool:
        # Live-only view (see _live_values): "in" must mean "still protected by
        # replace()", not "still sitting in the dict".
        return value in self._live_values()

    def __len__(self) -> int:
        # Same: status() publishes this as scrub_entries, i.e. the number of
        # actively protected plaintexts. Reporting the registry size here would
        # over-report entries no longer scrubbed.
        return len(self._live_values())
