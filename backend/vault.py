# -*- coding: utf-8 -*-
"""In-memory one-time handle store. Values never leave this process."""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict

_PREFIX = "sr_"
_MAX_SCAN = 256


class Vault:
    """Maps an opaque ref to the plaintext, consumable exactly once.

    Nothing is persisted: a restart drops every ref, which is the point
    (spec section 9) — the model then gets ref_unavailable and must ask again.
    """

    def __init__(self, ttl_seconds: int = 300, max_entries: int = 64) -> None:
        self._ttl = max(int(ttl_seconds), 1)
        self._max = max(int(max_entries), 1)
        # ref -> (value, monotonic issued at). OrderedDict keeps eviction
        # deterministic (oldest first) instead of relying on dict order luck.
        self._issued: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        # TTL-expired-but-never-consumed refs we still recognise, so a late
        # consumer can be told "expired" instead of a vague "unavailable"
        # (T3 distinguishes them). Consumed and evicted refs are *not* kept
        # here — they are intentionally unrecognisable ("gone").
        self._expired: set[str] = set()

    def issue(self, value: str) -> str:
        ref = _PREFIX + secrets.token_urlsafe(16)
        self._issued[ref] = (value, time.monotonic())
        while len(self._issued) > self._max:
            self._issued.popitem(last=False)
        return ref

    def _live(self, ref: str) -> tuple[str | None, str]:
        entry = self._issued.get(ref)
        if entry is None:
            return None, ("ref_expired" if self._was_known_and_expired(ref) else "ref_unavailable")
        value, at = entry
        if time.monotonic() - at > self._ttl:
            del self._issued[ref]
            self._expired.add(ref)
            return None, "ref_expired"
        return value, "ok"

    def _was_known_and_expired(self, ref: str) -> bool:
        return ref in self._expired

    def consume(self, ref: str) -> tuple[str | None, str]:
        value, status = self._live(ref)
        if status == "ok" and value is not None:
            del self._issued[ref]
            return value, "ok"
        return None, status

    def status(self, ref: str) -> str:
        if ref in self._expired:
            return "expired"
        if ref not in self._issued:
            return "gone"
        value, at = self._issued[ref]
        return "live" if time.monotonic() - at <= self._ttl else "expired"

    def sweep(self) -> int:
        now = time.monotonic()
        stale = [ref for ref, (_v, at) in self._issued.items() if now - at > self._ttl][:_MAX_SCAN]
        for ref in stale:
            self._issued.pop(ref, None)
            self._expired.add(ref)
        self._trim_expired()
        return len(stale)

    def _trim_expired(self) -> None:
        while len(self._expired) > self._max:
            self._expired.pop()

    def __len__(self) -> int:
        return len(self._issued)
