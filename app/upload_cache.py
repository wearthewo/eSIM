"""Checked uploads kept in process memory between /validate and the /generate parts.

After /validate creates or continues a batch, its rows go into ``UploadCache`` under the
batch id. /generate for a part of that batch then needs only the batch id and the part
number: no file upload, no parse, no whole-file validation. The part's rows are still
checked for duplicates inside the per-organization lock (``batches.record_part``).

The cached rows contain activation codes (and confirmation codes, names, emails). They
live only in this structure:

- never written to disk, logged, pickled or put in a database;
- ``repr``/``str`` of the cache and its entries show ids and counts only;
- an entry expires ``ttl_seconds`` after its last use, is evicted least-recently-used
  when the cache passes ``max_bytes``, and is discarded when its batch is completed,
  failed or voided. ``purge_expired``, which the app runs every minute, deletes entries
  that have expired or would before the next run, so none stays longer than
  ``ttl_seconds`` after its last use, even when no request comes.

A miss (expired, evicted, server restarted, another worker process) is not an error:
the page sends the file again for that part and the server fills the cache again.

Lookups are scoped to the organization: an entry is returned only when the caller's
organization owns it. There is one cache per app (``app.state.upload_cache``); it is
thread-safe, because routes call it from worker threads.

This module never imports ``app.web`` or the database layer.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.models import EsimRecord

_RECORD_FIELDS = tuple(f for f in EsimRecord.__slots__ if f != "row_number")
_POINTER_BYTES = 8  # one tuple slot per record


def estimate_bytes(records: Sequence[EsimRecord]) -> int:
    """Approximate memory held by ``records``: each record, its strings, the tuple."""
    total = sys.getsizeof(()) + _POINTER_BYTES * len(records)
    size_of = sys.getsizeof
    for record in records:
        total += size_of(record)
        for name in _RECORD_FIELDS:
            value = getattr(record, name)
            if value is not None:
                total += size_of(value)
    return total


@dataclass(frozen=True, slots=True, repr=False)
class CachedUpload:
    """The checked rows of one batch. Holds activation codes: never log or persist it."""

    organization_id: int
    batch_id: int
    row_count: int
    chunk_rows: int
    has_email: bool
    records: tuple[EsimRecord, ...]
    nbytes: int

    @classmethod
    def build(
        cls,
        *,
        organization_id: int,
        batch_id: int,
        chunk_rows: int,
        has_email: bool,
        records: Sequence[EsimRecord],
    ) -> CachedUpload:
        rows = tuple(records)
        return cls(
            organization_id=organization_id,
            batch_id=batch_id,
            row_count=len(rows),
            chunk_rows=chunk_rows,
            has_email=has_email,
            records=rows,
            nbytes=estimate_bytes(rows),
        )

    def __repr__(self) -> str:
        return (
            f"CachedUpload(organization_id={self.organization_id}, batch_id={self.batch_id}, "
            f"rows={self.row_count}, chunk_rows={self.chunk_rows}, nbytes={self.nbytes})"
        )

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise TypeError("cached uploads cannot be pickled")


class UploadCache:
    """Batch id -> ``CachedUpload``, capped at ``max_bytes`` (LRU) and ``ttl_seconds``
    after last use. ``max_bytes == 0`` turns the cache off (every lookup misses)."""

    def __init__(
        self,
        max_bytes: int,
        ttl_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_bytes < 0 or ttl_seconds <= 0:
            raise ValueError("max_bytes must be >= 0 and ttl_seconds > 0")
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # batch id -> (entry, last used); oldest use first
        self._entries: OrderedDict[int, tuple[CachedUpload, float]] = OrderedDict()
        self._bytes = 0

    def get(self, organization_id: int, batch_id: int) -> CachedUpload | None:
        """The entry if it exists, belongs to ``organization_id`` and has not expired.
        A hit counts as a use. A lookup for another organization changes nothing."""
        with self._lock:
            now = self._clock()
            found = self._entries.get(batch_id)
            if found is None:
                return None
            entry, last_used = found
            if entry.organization_id != organization_id:
                return None
            if now - last_used > self.ttl_seconds:
                self._remove(batch_id)
                return None
            self._entries[batch_id] = (entry, now)
            self._entries.move_to_end(batch_id)
            return entry

    def put(self, entry: CachedUpload) -> bool:
        """Store ``entry`` (replacing the batch's earlier one), then evict expired and
        least-recently-used entries until the cache fits. Returns False when the entry
        alone is larger than ``max_bytes`` (it is not stored)."""
        with self._lock:
            self._remove(entry.batch_id)
            if entry.nbytes > self.max_bytes:
                return False
            now = self._clock()
            self._entries[entry.batch_id] = (entry, now)
            self._bytes += entry.nbytes
            self._evict(now)
            return True

    def purge_expired(self, *, within: float = 0) -> int:
        """Delete every entry that has expired, or will within ``within`` seconds; returns
        how many. The app calls this every ``CACHE_SWEEP_SECONDS`` with ``within`` set to
        that interval (``app.web.main``), so no entry stays in memory longer than
        ``ttl_seconds`` after its last use, even when no request comes."""
        with self._lock:
            before = len(self._entries)
            self._evict(self._clock() + max(0.0, within))
            return before - len(self._entries)

    def discard(self, batch_id: int) -> None:
        """Forget the batch's rows (completed, failed or voided). No-op if absent."""
        with self._lock:
            self._remove(batch_id)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, batch_id: object) -> bool:
        with self._lock:
            return batch_id in self._entries

    @property
    def nbytes(self) -> int:
        """Estimated bytes held by all entries."""
        with self._lock:
            return self._bytes

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"UploadCache(entries={len(self._entries)}, nbytes={self._bytes}, "
                f"max_bytes={self.max_bytes}, ttl_seconds={self.ttl_seconds:g})"
            )

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise TypeError("the upload cache cannot be pickled")

    # --- with the lock held ---

    def _remove(self, batch_id: int) -> None:
        found = self._entries.pop(batch_id, None)
        if found is not None:
            self._bytes -= found[0].nbytes

    def _evict(self, now: float) -> None:
        expired = [
            batch_id
            for batch_id, (_, last_used) in self._entries.items()
            if now - last_used > self.ttl_seconds
        ]
        for batch_id in expired:
            self._remove(batch_id)
        while self._bytes > self.max_bytes and self._entries:
            oldest = next(iter(self._entries))
            self._remove(oldest)
