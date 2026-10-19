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

    Not thread-safe: keep issue/consume/peek/sweep on the host event loop (ruling
    R14).  Every read-modify-write here (`_live` deletes and re-marks, `consume`
    deletes after reading, `sweep`/`__len__` prune while walking) has no lock, and
    none is needed as long as one loop runs them — they contain no ``await``, so
    they are atomic *within* the loop.  Moving any of these calls into a thread,
    a thread pool or ``asyncio.to_thread`` would put two of them inside the same
    dict at once and hand back a plaintext twice (double-spend of a one-time ref).
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

    def peek(self, ref: str) -> str | None:
        """Non-consuming read for the scrubber's registration path only.

        Task 7 must tell "the value is still payable" apart from "the value is gone"
        without spending it (I3 says only the two consumer tools spend), and it needs
        the plaintext itself to add it to the egress scrub set (I4). This is an
        internal Python call, not a surface: no route, no tool result, no log ever
        carries what it returns (spec section 3 I2).

        Side effect is deliberate and identical to ``consume``'s first half: a
        TTL-expired ref is dropped and remembered, so it reads as ``ref_expired``
        later rather than as a vague ``ref_unavailable``.
        """
        value, status = self._live(ref)
        return value if status == "ok" else None

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
        # 裁定 R40（对齐裁定 R24 在 scrub 那一侧的修法）：这里的数字会被 Task 9 当作
        # `vault_size` 报给观察面，它必须等于"现在还兑得出值的条数"，而不是"字典里还剩
        # 几条"。TTL 过期但没人来消费的条目在 `_live()`/`sweep()` 之前一直躺在 `_issued`
        # 里，读原始 dict 就是虚报（Task 4 的评审在 scrub 上抓到过同一个缺陷）。
        # 过期在这里顺带回收：观察者的读就是它的清扫时机，不需要额外的周期任务。
        return self._live_count()

    def _live_count(self) -> int:
        now = time.monotonic()
        # 先快照再删：下面的 pop 动的是同一个 dict；`sweep()` 用同样的形状。
        expired = [ref for ref, (_v, at) in list(self._issued.items()) if now - at > self._ttl]
        for ref in expired:
            self._issued.pop(ref, None)
            self._expired.add(ref)
        self._trim_expired()
        return len(self._issued)
