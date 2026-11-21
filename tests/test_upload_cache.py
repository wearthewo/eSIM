"""app.upload_cache: hit, miss, organization scoping, expiry, LRU eviction, no leaks.

Synthetic records only (fake 8999 ICCIDs, smdp.example.invalid activation codes).
"""

from __future__ import annotations

import copy
import logging
import pickle
import tracemalloc

import pytest

from app.core.models import EsimRecord
from app.upload_cache import CachedUpload, UploadCache, estimate_bytes

ORG_A = 1
ORG_B = 2
TTL = 30 * 60


def _code(n: int) -> str:
    return f"LPA:1$smdp.example.invalid$CACHE-{n:06d}-SECRET"


def _records(count: int, start: int = 0) -> list[EsimRecord]:
    return [
        EsimRecord(
            row_number=n + 2,
            iccid=f"8999{n:015d}",
            activation_code=_code(n),
            confirmation_code=f"CONF{n:06d}",
            imsi=f"00101{n:010d}",
            msisdn=f"+3069{n:08d}",
            customer_name=f"Customer {n}",
            plan_name="Travel 10GB",
            email=f"customer{n}@mail.example.invalid",
        )
        for n in range(start, start + count)
    ]


def _entry(batch_id: int, organization_id: int = ORG_A, rows: int = 5) -> CachedUpload:
    return CachedUpload.build(
        organization_id=organization_id,
        batch_id=batch_id,
        chunk_rows=500,
        has_email=True,
        records=_records(rows, start=batch_id * 1000),
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cache(clock: Clock) -> UploadCache:
    return UploadCache(max_bytes=50 * 1024 * 1024, ttl_seconds=TTL, clock=clock)


def test_hit_returns_the_cached_rows(cache: UploadCache) -> None:
    entry = _entry(7)
    assert cache.put(entry)
    found = cache.get(ORG_A, 7)
    assert found is entry
    assert found.row_count == 5 and found.records == tuple(_records(5, start=7000))


def test_miss_for_an_unknown_batch(cache: UploadCache) -> None:
    cache.put(_entry(7))
    assert cache.get(ORG_A, 8) is None


def test_another_organizations_batch_id_never_hits(cache: UploadCache, clock: Clock) -> None:
    cache.put(_entry(7, organization_id=ORG_A))
    assert cache.get(ORG_B, 7) is None
    # The foreign lookup did not count as a use: the entry still expires on time.
    clock.now += TTL - 1
    assert cache.get(ORG_B, 7) is None
    clock.now += 2
    assert cache.get(ORG_A, 7) is None


def test_entry_expires_after_ttl_since_last_use(cache: UploadCache, clock: Clock) -> None:
    cache.put(_entry(7))
    clock.now += TTL - 1
    assert cache.get(ORG_A, 7) is not None  # a use: the TTL starts again
    clock.now += TTL - 1
    assert cache.get(ORG_A, 7) is not None
    clock.now += TTL + 1
    assert cache.get(ORG_A, 7) is None
    assert 7 not in cache and cache.nbytes == 0


def test_purge_expired_deletes_without_a_lookup(cache: UploadCache, clock: Clock) -> None:
    """The app's sweep: expired rows leave memory even if nobody asks for them again."""
    cache.put(_entry(1))
    clock.now += TTL / 2
    cache.put(_entry(2))
    clock.now += TTL / 2 + 1  # 1 has expired, 2 has not
    assert cache.purge_expired() == 1
    assert 1 not in cache and 2 in cache
    assert cache.nbytes == _entry(2).nbytes
    assert cache.purge_expired() == 0


def test_purge_expired_within_a_margin(cache: UploadCache, clock: Clock) -> None:
    """With ``within`` (the sweep interval) an entry that would expire before the next
    sweep is deleted now, so none outlives the TTL."""
    cache.put(_entry(1))
    clock.now += TTL - 30  # expires in 30 s
    assert cache.purge_expired(within=10) == 0
    assert cache.purge_expired(within=60) == 1
    assert 1 not in cache and cache.nbytes == 0


def test_expired_entries_are_dropped_on_put(cache: UploadCache, clock: Clock) -> None:
    cache.put(_entry(1))
    clock.now += TTL + 1
    cache.put(_entry(2))
    assert 1 not in cache and 2 in cache
    assert cache.nbytes == _entry(2).nbytes


def test_least_recently_used_is_evicted_over_the_cap(clock: Clock) -> None:
    size = _entry(1).nbytes
    cache = UploadCache(max_bytes=int(size * 2.5), ttl_seconds=TTL, clock=clock)
    cache.put(_entry(1))
    cache.put(_entry(2))
    assert cache.get(ORG_A, 1) is not None  # 1 is now more recently used than 2
    cache.put(_entry(3))
    assert 2 not in cache  # evicted
    assert cache.get(ORG_A, 1) is not None and cache.get(ORG_A, 3) is not None
    assert cache.nbytes <= cache.max_bytes


def test_entry_larger_than_the_cap_is_not_stored(clock: Clock) -> None:
    cache = UploadCache(max_bytes=1000, ttl_seconds=TTL, clock=clock)
    assert not cache.put(_entry(1, rows=50))
    assert len(cache) == 0 and cache.get(ORG_A, 1) is None


def test_zero_bytes_turns_the_cache_off(clock: Clock) -> None:
    cache = UploadCache(max_bytes=0, ttl_seconds=TTL, clock=clock)
    assert not cache.put(_entry(1))
    assert cache.get(ORG_A, 1) is None


def test_put_replaces_the_batch_entry(cache: UploadCache) -> None:
    cache.put(_entry(7, rows=5))
    bigger = _entry(7, rows=9)
    cache.put(bigger)
    assert len(cache) == 1 and cache.nbytes == bigger.nbytes
    assert cache.get(ORG_A, 7) is bigger


def test_discard_and_clear(cache: UploadCache) -> None:
    cache.put(_entry(1))
    cache.put(_entry(2))
    cache.discard(1)
    cache.discard(99)  # absent: no-op
    assert 1 not in cache and 2 in cache
    cache.clear()
    assert len(cache) == 0 and cache.nbytes == 0


def test_bad_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        UploadCache(max_bytes=-1, ttl_seconds=TTL)
    with pytest.raises(ValueError):
        UploadCache(max_bytes=1, ttl_seconds=0)


def test_repr_str_and_logs_never_contain_codes(
    cache: UploadCache, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _entry(7)
    cache.put(entry)
    shown = [repr(cache), str(cache), repr(entry), str(entry), f"{entry}", f"{cache!r}"]
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("cache %s entry %r", cache, entry)
    shown.append(caplog.text)
    for text in shown:
        for record in entry.records:
            assert record.activation_code not in text
            assert record.confirmation_code not in text
            assert record.email not in text
    assert "batch_id=7" in repr(entry) and "rows=5" in repr(entry)


def test_entries_and_cache_cannot_be_pickled_or_copied(cache: UploadCache) -> None:
    entry = _entry(7)
    cache.put(entry)
    for value in (entry, cache):
        with pytest.raises(TypeError):
            pickle.dumps(value)
        with pytest.raises(TypeError):
            copy.deepcopy(value)


def test_size_estimate_matches_tracemalloc() -> None:
    # Measure what building 2,000 records really allocates, then compare the estimate.
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        records = tuple(_records(2000))
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    measured = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    estimated = estimate_bytes(records)
    assert measured > 0
    assert 0.75 <= estimated / measured <= 1.25, (estimated, measured)
