"""
Lightweight in-memory TTL cache.

No external service required. Safe for use inside a single asyncio event loop
(the skill runs as one process). Provides TTL expiration plus a simple
max-size LRU eviction so memory stays bounded.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class _CacheEntry:
    value: Any
    expires_at: float


class TTLCache:
    def __init__(self, default_ttl: float = 60.0, maxsize: int = 1000):
        """
        Args:
            default_ttl: Default time-to-live in seconds.
            maxsize: Maximum number of entries before LRU eviction.
        """
        self.default_ttl = default_ttl
        self.maxsize = maxsize
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()

    def _now(self) -> float:
        return time.monotonic()

    def _evict_expired(self) -> None:
        now = self._now()
        expired = [k for k, e in self._store.items() if e.expires_at <= now]
        for k in expired:
            self._store.pop(k, None)

    def _enforce_size(self) -> None:
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)

    def get(self, key: str, default: Any = None) -> Any:
        self._evict_expired()
        entry = self._store.get(key)
        if entry is None:
            return default
        if entry.expires_at <= self._now():
            self._store.pop(key, None)
            return default
        # Move to the end (most recently used).
        self._store.move_to_end(key)
        return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._evict_expired()
        expires_at = self._now() + (ttl if ttl is not None else self.default_ttl)
        self._store[key] = _CacheEntry(value=value, expires_at=expires_at)
        self._store.move_to_end(key)
        self._enforce_size()

    def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        self._store.clear()

    def info(self) -> dict[str, Any]:
        self._evict_expired()
        return {"size": len(self._store), "maxsize": self.maxsize, "default_ttl": self.default_ttl}


def make_key(prefix: str, *args: Any, **kwargs: Any) -> str:
    """Build a deterministic cache key from function arguments."""
    parts = [prefix]
    if args:
        parts.append(repr(args))
    if kwargs:
        # Sort keys for stable hashing.
        parts.append(repr(sorted(kwargs.items())))
    return "|".join(parts)
