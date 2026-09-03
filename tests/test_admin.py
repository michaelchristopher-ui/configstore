"""The write path, against a real etcd. Marked `etcd`; skipped without one."""

from __future__ import annotations

import pytest

from configstore.admin import ConfigAdmin, Conflict
from configstore.etcd import Etcd
from configstore.schema import Schema, SchemaError, ValidationError

pytestmark = pytest.mark.etcd


def test_set_then_read(admin: ConfigAdmin, app: str) -> None:
    admin.set(app, "prod", "retrieval.top_k", 12)
    assert admin.read_layer(app, "prod")["retrieval.top_k"].json() == 12


def test_a_write_built_on_a_stale_read_is_refused(admin: ConfigAdmin, app: str) -> None:
    admin.set(app, "prod", "retrieval.top_k", 12)
    stale = admin.current(app, "prod", "retrieval.top_k")
    assert stale is not None
    admin.set(app, "prod", "retrieval.top_k", 13)  # somebody else got there first
    with pytest.raises(Conflict) as caught:
        admin.set(app, "prod", "retrieval.top_k", 99, expect=stale.mod_revision)
    assert caught.value.current is not None
    assert caught.value.current.json() == 13  # and the winner's value stands


def test_creation_races_are_also_serialised(admin: ConfigAdmin, app: str) -> None:
    # `expect=0` means "I believe this key does not exist". Two creators cannot
    # both succeed, which is what stops a first write from being lost.
    admin.set(app, "prod", "chat.model", "a", expect=0)
    with pytest.raises(Conflict):
        admin.set(app, "prod", "chat.model", "b", expect=0)


def test_apply_is_one_atomic_change(admin: ConfigAdmin, app: str) -> None:
    revision = admin.apply(
        app,
        "prod",
        {"retrieval.top_k": 20, "retrieval.rrf_k": 90, "judge.enabled": True},
    )
    live = admin.read_layer(app, "prod")
    # One revision for every key: no window in which half the change is live.
    assert {k: v.mod_revision for k, v in live.items()} == {
        "retrieval.top_k": revision,
        "retrieval.rrf_k": revision,
        "judge.enabled": revision,
    }


def test_apply_rejects_the_whole_change_if_any_key_moved(
    admin: ConfigAdmin, app: str
) -> None:
    admin.set(app, "prod", "retrieval.top_k", 1)
    stale = admin.read_layer(app, "prod")["retrieval.top_k"].mod_revision
    admin.set(app, "prod", "retrieval.top_k", 2)
    with pytest.raises(Conflict):
        admin.apply(
            app,
            "prod",
            {"retrieval.top_k": 3, "retrieval.rrf_k": 70},
            expect={"retrieval.top_k": stale},
        )
    # and nothing from the refused change is live
    assert "retrieval.rrf_k" not in admin.read_layer(app, "prod")


def test_history_records_every_change_with_its_author(
    admin: ConfigAdmin, app: str
) -> None:
    admin.set(app, "prod", "retrieval.top_k", 1)
    admin.set(app, "prod", "retrieval.top_k", 2)
    admin.unset(app, "prod", "retrieval.top_k")
    entries = admin.history(app, "prod", "retrieval.top_k")
    assert [(e.op, e.previous, e.value) for e in entries] == [
        ("set", None, 1),
        ("set", 1, 2),
        ("unset", 2, None),
    ]
    assert {e.actor for e in entries} == {"tests@example.com"}
    # oldest first, numbered from 1
    assert [e.sequence for e in entries] == [1, 2, 3]


def test_history_survives_compaction(admin: ConfigAdmin, app: str, etcd: Etcd) -> None:
    # The point of keeping history in ordinary keys rather than relying on
    # etcd's MVCC: compaction destroys MVCC history, and compaction is not
    # optional on a store that must not fill its backend quota.
    admin.set(app, "prod", "retrieval.top_k", 1)
    revision = admin.set(app, "prod", "retrieval.top_k", 2)
    etcd.compact(revision)
    entries = admin.history(app, "prod", "retrieval.top_k")
    assert [e.value for e in entries] == [1, 2]


def test_rollback_appends_rather_than_rewrites(admin: ConfigAdmin, app: str) -> None:
    admin.set(app, "prod", "retrieval.top_k", 1)
    admin.set(app, "prod", "retrieval.top_k", 2)
    target = admin.history(app, "prod", "retrieval.top_k")[0]
    admin.rollback(app, "prod", "retrieval.top_k", to=target.sequence)
    assert admin.read_layer(app, "prod")["retrieval.top_k"].json() == 1
    # the rollback is itself a recorded change; nothing was erased
    assert [e.value for e in admin.history(app, "prod", "retrieval.top_k")] == [1, 2, 1]


def test_unset_keeps_the_removed_value_in_history(admin: ConfigAdmin, app: str) -> None:
    admin.set(app, "prod", "retrieval.top_k", 42)
    admin.unset(app, "prod", "retrieval.top_k")
    assert "retrieval.top_k" not in admin.read_layer(app, "prod")
    assert admin.history(app, "prod", "retrieval.top_k")[-1].previous == 42


def test_writes_are_validated_against_the_published_schema(
    admin: ConfigAdmin, app: str
) -> None:
    with pytest.raises(ValidationError):
        admin.set(app, "prod", "retrieval.top_k", "twelve")
    with pytest.raises(ValidationError):
        admin.set(app, "prod", "api.key", "sk-live-9f3ac2")


def test_writing_without_a_schema_is_refused(etcd: Etcd) -> None:
    writer = ConfigAdmin(etcd, actor="tests")
    with pytest.raises(SchemaError):
        writer.set("neverpublished", "prod", "a.b", 1)


def test_schema_round_trips_through_the_store(
    admin: ConfigAdmin, schema: Schema
) -> None:
    assert admin.read_schema(schema.app) == schema


def test_every_write_is_attributed(etcd: Etcd) -> None:
    with pytest.raises(ValueError):
        ConfigAdmin(etcd, actor="  ")


def test_recreating_a_removed_key_does_not_overwrite_its_first_entry(
    admin: ConfigAdmin, app: str
) -> None:
    # The bug a revision-keyed history had: a creation supersedes nothing, so
    # every creation claimed the same slot and recreating a removed key
    # displaced the original creation's record.
    admin.set(app, "prod", "retrieval.top_k", 1)
    admin.unset(app, "prod", "retrieval.top_k")
    admin.set(app, "prod", "retrieval.top_k", 2)
    entries = admin.history(app, "prod", "retrieval.top_k")
    assert [(e.sequence, e.op, e.value) for e in entries] == [
        (1, "set", 1),
        (2, "unset", None),
        (3, "set", 2),
    ]


def test_rollback_of_a_removal_removes_again(admin: ConfigAdmin, app: str) -> None:
    admin.set(app, "prod", "retrieval.top_k", 1)
    admin.unset(app, "prod", "retrieval.top_k")
    admin.set(app, "prod", "retrieval.top_k", 5)
    removal = next(
        e for e in admin.history(app, "prod", "retrieval.top_k") if e.op == "unset"
    )
    admin.rollback(app, "prod", "retrieval.top_k", to=removal.sequence)
    assert "retrieval.top_k" not in admin.read_layer(app, "prod")
