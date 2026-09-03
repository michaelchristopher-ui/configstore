"""The transport. Marked `etcd` where it needs a server; the rest is pure."""

from __future__ import annotations

import json

import pytest

from configstore.etcd import (
    MAX_VALUE_BYTES,
    Etcd,
    EtcdError,
    EtcdUnavailable,
    prefix_end,
)


def test_prefix_end_increments_the_final_byte() -> None:
    assert prefix_end("/cfg/v1/a/") == b"/cfg/v1/a0"
    assert prefix_end("a") == b"b"


def test_an_unreachable_endpoint_is_distinguishable_from_a_refusal() -> None:
    # The client's fallback path turns on this distinction: unreachable means
    # "serve the cache", while a refusal means the request was wrong.
    with pytest.raises(EtcdUnavailable):
        Etcd("http://127.0.0.1:1", timeout=0.3).status()


@pytest.mark.etcd
def test_a_range_reports_the_revision_it_read_at(etcd: Etcd, app: str) -> None:
    # The field `etcd3gw` discards, and the reason this module exists.
    revision = etcd.put(f"/cfg/v1/{app}/base/a", b"1")
    result = etcd.range(f"/cfg/v1/{app}/", prefix=True)
    assert result.revision >= revision
    assert result.as_dict()[f"/cfg/v1/{app}/base/a"].json() == 1


@pytest.mark.etcd
def test_a_prefix_range_is_one_atomic_snapshot(etcd: Etcd, app: str) -> None:
    # Two layers read in one range, which is why resolution needs no revision
    # pinning across reads.
    etcd.put(f"/cfg/v1/{app}/base/a", b"1")
    etcd.put(f"/cfg/v1/{app}/prod/a", b"2")
    result = etcd.range(f"/cfg/v1/{app}/", prefix=True)
    assert len(result.kvs) == 2
    assert all(kv.mod_revision <= result.revision for kv in result.kvs)


@pytest.mark.etcd
def test_a_revision_read_sees_the_past(etcd: Etcd, app: str) -> None:
    first = etcd.put(f"/cfg/v1/{app}/base/a", b"1")
    etcd.put(f"/cfg/v1/{app}/base/b", b"2")
    past = etcd.range(f"/cfg/v1/{app}/", prefix=True, revision=first)
    assert [kv.key for kv in past.kvs] == [f"/cfg/v1/{app}/base/a"]


@pytest.mark.etcd
def test_a_value_over_the_ceiling_is_refused_before_it_is_sent(
    etcd: Etcd, app: str
) -> None:
    # Refused here rather than by etcd's --max-request-bytes, because the point
    # is to catch a payload that belongs in object storage, not to discover the
    # cluster's limit.
    with pytest.raises(EtcdError, match="ceiling"):
        etcd.put(f"/cfg/v1/{app}/base/a", b"x" * (MAX_VALUE_BYTES + 1))


@pytest.mark.etcd
def test_deletes_report_the_revision_of_the_delete(etcd: Etcd, app: str) -> None:
    # The watch cursor depends on this: a DELETE event's kv carries the
    # revision at which the deletion happened, so it is a valid resume point.
    etcd.put(f"/cfg/v1/{app}/base/a", b"1")
    revision = etcd.delete(f"/cfg/v1/{app}/base/a")
    assert not etcd.range(f"/cfg/v1/{app}/base/a").kvs
    assert revision > 0


@pytest.mark.etcd
def test_a_malformed_request_raises_rather_than_returning_empty(etcd: Etcd) -> None:
    with pytest.raises(EtcdError):
        # A revision in the future is not a thing etcd will read.
        etcd.range("/cfg/v1/", prefix=True, revision=2**62)


@pytest.mark.etcd
def test_json_values_round_trip(etcd: Etcd, app: str) -> None:
    payload = {"b": [1, 2, {"c": True}], "a": None}
    etcd.put(f"/cfg/v1/{app}/base/a", json.dumps(payload).encode())
    assert etcd.range(f"/cfg/v1/{app}/base/a").kvs[0].json() == payload
