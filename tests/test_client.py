"""The read path, against a real etcd. Marked `etcd`; skipped without one."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from configstore.admin import ConfigAdmin
from configstore.client import ConfigClient
from configstore.etcd import Etcd
from configstore.schema import Schema
from configstore.snapshot import Snapshot

pytestmark = pytest.mark.etcd


def until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Wait for a watch to deliver. Polling the *client*, never the store."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def client(
    etcd: Etcd, app: str, schema: Schema, tmp_path: Path
) -> Iterator[ConfigClient]:
    running = ConfigClient(
        etcd,
        app=app,
        env="prod",
        schema=schema,
        cache_path=tmp_path / "cache.json",
    )
    running.start()
    yield running
    running.stop()


def test_resolves_layers_and_defaults(
    admin: ConfigAdmin, app: str, client: ConfigClient
) -> None:
    admin.set(app, "base", "retrieval.top_k", 10)
    admin.set(app, "prod", "retrieval.top_k", 12)
    client.refresh()
    assert client.get("retrieval.top_k") == 12
    assert client.get("retrieval.rrf_k") == 60  # schema default, no layer holds it
    assert client.snapshot.overridden() == ("retrieval.top_k",)


def test_a_change_is_pushed_without_polling(
    admin: ConfigAdmin, app: str, client: ConfigClient
) -> None:
    admin.set(app, "prod", "retrieval.top_k", 15)
    assert until(lambda: client.get("retrieval.top_k") == 15)


def test_a_multi_key_change_is_never_seen_half_applied(
    admin: ConfigAdmin, app: str, client: ConfigClient
) -> None:
    seen: list[tuple[int, bool]] = []
    client._on_change = lambda snap: seen.append(
        (snap.get("retrieval.top_k"), snap.get("judge.enabled"))
    )
    admin.apply(app, "prod", {"retrieval.top_k": 30, "judge.enabled": True})
    assert until(lambda: client.get("judge.enabled") is True)
    # Every state the application was ever able to observe. The pair changed
    # together, so no observation has one without the other.
    assert (30, True) in seen
    assert (30, False) not in seen


def test_removing_an_override_falls_back_to_base(
    admin: ConfigAdmin, app: str, client: ConfigClient
) -> None:
    admin.set(app, "base", "retrieval.top_k", 8)
    admin.set(app, "prod", "retrieval.top_k", 12)
    assert until(lambda: client.get("retrieval.top_k") == 12)
    admin.unset(app, "prod", "retrieval.top_k")
    # Rebuilt from raw keys rather than patched, so the base value comes back
    # instead of the deleted override lingering.
    assert until(lambda: client.get("retrieval.top_k") == 8)
    assert client.snapshot.sources["retrieval.top_k"] == "base"


def test_a_compacted_resume_point_triggers_a_full_reread(
    etcd: Etcd, admin: ConfigAdmin, app: str, schema: Schema
) -> None:
    # The classic etcd watch bug: resuming from a revision that compaction has
    # destroyed. A watcher that treats the cancellation as transient retries
    # forever and serves stale configuration with no error anywhere.
    detached = ConfigClient(etcd, app=app, env="prod", schema=schema, watch=False)
    detached.start()
    stale_revision = detached.revision

    for value in range(1, 16):
        revision = admin.set(app, "prod", "retrieval.top_k", value)
    etcd.compact(revision)

    detached._revision = stale_revision  # as if it had been disconnected throughout
    watcher = threading.Thread(target=detached._watch_forever, daemon=True)
    watcher.start()
    try:
        assert until(lambda: detached.get("retrieval.top_k") == 15)
        assert detached.revision >= revision
    finally:
        detached.stop()


def test_the_create_acknowledgement_does_not_advance_the_cursor(
    etcd: Etcd, admin: ConfigAdmin, app: str
) -> None:
    # The first message on a watch stream carries the *store's* revision with no
    # events. Treating it as progress skips every change between the requested
    # start and now — silently, and permanently if the stream then breaks.
    admin.set(app, "prod", "retrieval.top_k", 1)
    start = admin.current(app, "prod", "retrieval.top_k")
    assert start is not None
    for value in (2, 3, 4):
        admin.set(app, "prod", "retrieval.top_k", value)

    acknowledged: list[int] = []
    delivered: list[int] = []
    for batch in etcd.clone().watch(
        f"/cfg/v1/{app}/", start_revision=start.mod_revision + 1
    ):
        if batch.created:
            acknowledged.append(batch.revision)
            continue
        delivered.extend(event.kv.mod_revision for event in batch.events)
        break
    # The ack ran ahead of the first change the watch actually delivered.
    assert acknowledged[0] > min(delivered)
    assert min(delivered) == start.mod_revision + 1


def test_an_unreachable_etcd_does_not_stop_an_application_from_starting(
    admin: ConfigAdmin, app: str, client: ConfigClient, schema: Schema, tmp_path: Path
) -> None:
    admin.set(app, "prod", "retrieval.top_k", 21)
    assert until(lambda: client.get("retrieval.top_k") == 21)
    client.stop()

    offline = ConfigClient(
        Etcd("http://127.0.0.1:1", timeout=0.3),
        app=app,
        env="prod",
        schema=schema,
        cache_path=tmp_path / "cache.json",
        watch=False,
    )
    snapshot = offline.start()
    assert snapshot.stale is True
    assert snapshot.get("retrieval.top_k") == 21


def test_a_cache_from_another_environment_is_refused(
    admin: ConfigAdmin, app: str, client: ConfigClient, schema: Schema, tmp_path: Path
) -> None:
    # Starting prod on staging's configuration is worse than not starting.
    admin.set(app, "prod", "retrieval.top_k", 21)
    assert until(lambda: client.get("retrieval.top_k") == 21)
    client.stop()

    from configstore.etcd import EtcdUnavailable

    wrong = ConfigClient(
        Etcd("http://127.0.0.1:1", timeout=0.3),
        app=app,
        env="staging",
        schema=schema,
        cache_path=tmp_path / "cache.json",
        watch=False,
    )
    with pytest.raises(EtcdUnavailable):
        wrong.start()


def test_reads_do_not_require_start_to_have_succeeded_silently(
    etcd: Etcd, app: str, schema: Schema
) -> None:
    never_started = ConfigClient(etcd, app=app, env="prod", schema=schema, watch=False)
    with pytest.raises(RuntimeError):
        never_started.get("retrieval.top_k")


def test_a_raising_callback_does_not_kill_the_watch(
    admin: ConfigAdmin, app: str, client: ConfigClient
) -> None:
    def explode(_: Snapshot) -> None:
        raise RuntimeError("application bug")

    client._on_change = explode
    admin.set(app, "prod", "retrieval.top_k", 77)
    assert until(lambda: client.get("retrieval.top_k") == 77)
    admin.set(app, "prod", "retrieval.top_k", 78)
    assert until(lambda: client.get("retrieval.top_k") == 78)
