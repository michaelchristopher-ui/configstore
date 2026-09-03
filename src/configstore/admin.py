"""The write path: validated, atomic, compare-and-swapped, and audited.

Three properties, and each of them is something the shape "HTTP service in front
of a table" has to be careful to build and this gets from the store:

- **Every write states what it replaces.** There is no unconditional `set`. A
  write carries the `mod_revision` it believes is current, etcd compares it
  inside the transaction, and a write built on a stale read is refused rather
  than applied. Two people editing the same key in two browser tabs cannot
  silently overwrite each other — the second one is told. `UPDATE … WHERE id = ?`
  has no such property unless someone remembers to add a version column and
  every writer remembers to use it.
- **A change to several keys is one event.** `apply` puts every key in one
  transaction, so watchers see all of it or none of it. Sequential writes
  through an API leave a window in which half the new configuration is live,
  and that window is where the interesting outages come from.
- **History is written in the same transaction as the value.** Not afterwards,
  not by a trigger, not best-effort. An audit trail that can be missing entries
  is not an audit trail, and this one cannot be: the value and its history entry
  land together or the transaction fails. It cannot be *overwritten* either —
  every transaction asserts that the history slot it is claiming is empty, so
  no code path, including recreating a key that was removed, can displace a
  recorded change.

**Why history is not just etcd's MVCC.** etcd already keeps every revision, and
`range(revision=N)` reads the past directly — for a while. Compaction deletes
that history, and compaction is mandatory: skip it and the backend grows until
it trips the quota alarm and the cluster goes read-only. So MVCC history is a
window, not a record. `/hist/` keys are ordinary keys that survive compaction,
which is what makes "who changed `judge.enabled` in March" answerable.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .etcd import Etcd, KeyValue
from .layout import (
    CFG_PREFIX,
    app_prefix,
    check_key,
    check_layer,
    config_key,
    history_key,
    history_prefix,
    history_sequence,
    layer_prefix,
    schema_key,
)
from .schema import Schema, SchemaError, empty

# etcd refuses a transaction with more than --max-txn-ops operations (128 by
# default). Every key in an apply costs two — the value and its history entry —
# so this is the honest ceiling, kept below the limit rather than at it.
MAX_APPLY_KEYS = 60


class Conflict(RuntimeError):
    """The value changed since it was read; the write was refused, not applied.

    Carries what is current so a caller can show a diff rather than just losing
    the edit — the console reloads from `current` and asks.
    """

    def __init__(self, key: str, expected: int, current: KeyValue | None) -> None:
        actual = current.mod_revision if current else 0
        super().__init__(
            f"{key} is at revision {actual}, not the {expected} this write "
            "expected — it was changed by someone else in the meantime"
        )
        self.key = key
        self.expected = expected
        self.current = current


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One recorded change to one key.

    `sequence` is the entry's identity and its order — the Nth change to this
    key — and what `rollback(to=...)` names. `replaces` is the revision the
    change superseded, kept as a field so an entry can still be correlated with
    an etcd revision, but not used as the key: see `layout`. `value` is what
    became live; `previous` is what it displaced, which is what makes an undo
    possible without reading the neighbouring entry.
    """

    layer: str
    key: str
    sequence: int
    op: str
    value: Any
    previous: Any
    actor: str
    at: str
    replaces: int

    @classmethod
    def from_kv(cls, kv: KeyValue, *, layer: str, key: str) -> HistoryEntry:
        body = kv.json()
        return cls(
            layer=layer,
            key=key,
            sequence=history_sequence(kv.key),
            op=str(body.get("op", "set")),
            value=body.get("value"),
            previous=body.get("previous"),
            actor=str(body.get("actor", "")),
            at=str(body.get("at", "")),
            replaces=int(body.get("replaces", 0)),
        )


def _b64(raw: str | bytes) -> str:
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _put_op(key: str, value: bytes) -> dict[str, Any]:
    return {"request_put": {"key": _b64(key), "value": _b64(value)}}


def _delete_op(key: str) -> dict[str, Any]:
    return {"request_delete_range": {"key": _b64(key)}}


def _expect_absent(key: str) -> dict[str, Any]:
    """Assert a key has never been written — `version` is 0 for such a key.

    Used on history slots, so a change can never displace a recorded one.
    """
    return {
        "key": _b64(key),
        "target": "VERSION",
        "result": "EQUAL",
        "version": "0",
    }


def _expect_mod(key: str, revision: int) -> dict[str, Any]:
    """Compare a key's `mod_revision`, where 0 means "must not exist".

    etcd reports `mod_revision` 0 for a key that was never written, so the
    creation case needs no separate comparison — the same clause that guards an
    update guards a create against another create.
    """
    return {
        "key": _b64(key),
        "target": "MOD",
        "result": "EQUAL",
        "mod_revision": str(revision),
    }


class ConfigAdmin:
    """Writes, on behalf of a named actor.

    `actor` is recorded on every history entry and is required: an audit trail
    of anonymous changes answers the least interesting half of the question.
    """

    def __init__(self, etcd: Etcd, *, actor: str, validate: bool = True) -> None:
        if not actor.strip():
            raise ValueError("every write is attributed; `actor` cannot be empty")
        self._etcd = etcd
        self._actor = actor
        self._validate = validate

    # -- schema ------------------------------------------------------------

    def publish_schema(self, schema: Schema) -> int:
        """Store an app's declared keys, for the console and for validation."""
        return self._etcd.put(schema_key(schema.app), schema.to_bytes())

    def read_schema(self, app: str) -> Schema:
        result = self._etcd.range(schema_key(app))
        if not result.kvs:
            return empty(app)
        return Schema.from_bytes(result.kvs[0].value)

    def _schema_for(self, app: str) -> Schema:
        schema = self.read_schema(app)
        if self._validate and not schema.keys:
            raise SchemaError(
                f"no schema is published for {app!r}, so a write cannot be "
                "checked against anything — publish one first "
                "(`configstore push-schema`), or build ConfigAdmin with "
                "validate=False to bootstrap deliberately"
            )
        return schema

    # -- reads (for the console) -------------------------------------------

    def read_layer(self, app: str, layer: str) -> dict[str, KeyValue]:
        """One layer's keys, keyed by config key rather than by etcd key."""
        prefix = layer_prefix(app, layer)
        result = self._etcd.range(prefix, prefix=True)
        return {kv.key[len(prefix) :]: kv for kv in result.kvs}

    def apps(self) -> tuple[str, ...]:
        result = self._etcd.range(CFG_PREFIX, prefix=True, keys_only=True)
        return tuple(
            sorted({kv.key[len(CFG_PREFIX) :].split("/")[0] for kv in result.kvs})
        )

    def layers(self, app: str) -> tuple[str, ...]:
        prefix = app_prefix(app)
        result = self._etcd.range(prefix, prefix=True, keys_only=True)
        return tuple(sorted({kv.key[len(prefix) :].split("/")[0] for kv in result.kvs}))

    def current(self, app: str, layer: str, key: str) -> KeyValue | None:
        result = self._etcd.range(config_key(app, layer, key))
        return result.kvs[0] if result.kvs else None

    # -- writes ------------------------------------------------------------

    def set(
        self,
        app: str,
        layer: str,
        key: str,
        value: Any,
        *,
        expect: int | None = None,
    ) -> int:
        """Write one key. Returns the revision the change landed at.

        `expect` is the `mod_revision` the caller believes is live — pass what a
        read returned and a concurrent change makes this raise `Conflict`
        instead of overwriting. Omitting it reads the current revision first,
        which narrows the race to microseconds but does not close it; a UI
        should always pass what it displayed.
        """
        check_layer(layer)
        schema = self._schema_for(app)
        coerced = schema.coerce(check_key(key), value)
        live = self.current(app, layer, key)
        previous = live.json() if live else None
        replaces = live.mod_revision if live else 0
        if expect is not None and expect != replaces:
            raise Conflict(config_key(app, layer, key), expect, live)

        sequence = self._next_sequence(app, layer, key)
        ok, revision, _ = self._etcd.txn(
            compare=[
                _expect_mod(config_key(app, layer, key), replaces),
                _expect_absent(history_key(app, layer, key, sequence)),
            ],
            success=[
                _put_op(config_key(app, layer, key), _json(coerced)),
                _put_op(
                    history_key(app, layer, key, sequence),
                    self._record("set", coerced, previous, replaces, sequence),
                ),
            ],
        )
        if not ok:
            raise Conflict(
                config_key(app, layer, key), replaces, self.current(app, layer, key)
            )
        return revision

    def unset(
        self, app: str, layer: str, key: str, *, expect: int | None = None
    ) -> int:
        """Remove one key from one layer, recording what was removed.

        Removing an environment override is how a value falls back to the base
        layer, so this is an ordinary edit rather than a destructive one — and
        the removed value is kept in history, so nothing is actually lost.
        """
        check_layer(layer)
        live = self.current(app, layer, key)
        if live is None:
            raise KeyError(f"{config_key(app, layer, key)} does not exist")
        if expect is not None and expect != live.mod_revision:
            raise Conflict(config_key(app, layer, key), expect, live)

        sequence = self._next_sequence(app, layer, key)
        ok, revision, _ = self._etcd.txn(
            compare=[
                _expect_mod(config_key(app, layer, key), live.mod_revision),
                _expect_absent(history_key(app, layer, key, sequence)),
            ],
            success=[
                _delete_op(config_key(app, layer, key)),
                _put_op(
                    history_key(app, layer, key, sequence),
                    self._record(
                        "unset", None, live.json(), live.mod_revision, sequence
                    ),
                ),
            ],
        )
        if not ok:
            raise Conflict(
                config_key(app, layer, key),
                live.mod_revision,
                self.current(app, layer, key),
            )
        return revision

    def apply(
        self,
        app: str,
        layer: str,
        values: Mapping[str, Any],
        *,
        expect: Mapping[str, int] | None = None,
    ) -> int:
        """Write several keys as one atomic change.

        The reason this exists rather than a loop over `set`: keys that must
        change together — an endpoint and the credential reference that goes
        with it, a model and the collection it indexes into — are exactly the
        keys where a watcher observing an intermediate state does the wrong
        thing. One transaction, one watch batch, no intermediate state.
        """
        check_layer(layer)
        if not values:
            raise ValueError("apply needs at least one key")
        if len(values) > MAX_APPLY_KEYS:
            raise ValueError(
                f"{len(values)} keys exceeds the {MAX_APPLY_KEYS}-key ceiling for "
                "one transaction (etcd's --max-txn-ops, two operations per key)"
            )
        schema = self._schema_for(app)
        coerced = {check_key(k): schema.coerce(k, v) for k, v in values.items()}

        live = self.read_layer(app, layer)
        compare: list[dict[str, Any]] = []
        success: list[dict[str, Any]] = []
        for key, value in sorted(coerced.items()):
            existing = live.get(key)
            replaces = existing.mod_revision if existing else 0
            if expect is not None and key in expect and expect[key] != replaces:
                raise Conflict(config_key(app, layer, key), expect[key], existing)
            sequence = self._next_sequence(app, layer, key)
            compare.append(_expect_mod(config_key(app, layer, key), replaces))
            compare.append(_expect_absent(history_key(app, layer, key, sequence)))
            success.append(_put_op(config_key(app, layer, key), _json(value)))
            success.append(
                _put_op(
                    history_key(app, layer, key, sequence),
                    self._record(
                        "set",
                        value,
                        existing.json() if existing else None,
                        replaces,
                        sequence,
                    ),
                )
            )

        ok, revision, _ = self._etcd.txn(compare=compare, success=success)
        if not ok:
            # Which key lost the race is worth naming, so re-read and report the
            # first disagreement rather than a generic failure.
            fresh = self.read_layer(app, layer)
            for key in sorted(coerced):
                was = live.get(key)
                now = fresh.get(key)
                if (was.mod_revision if was else 0) != (now.mod_revision if now else 0):
                    raise Conflict(
                        config_key(app, layer, key),
                        was.mod_revision if was else 0,
                        now,
                    )
            raise Conflict(f"{layer_prefix(app, layer)}*", revision, None)
        return revision

    # -- history -----------------------------------------------------------

    def history(
        self, app: str, layer: str, key: str, *, limit: int = 0
    ) -> tuple[HistoryEntry, ...]:
        """Every recorded change to one key, oldest first."""
        result = self._etcd.range(
            history_prefix(app, layer, key),
            prefix=True,
            limit=limit,
            sort_order="ASCEND",
            sort_target="KEY",
        )
        return tuple(
            HistoryEntry.from_kv(kv, layer=check_layer(layer), key=check_key(key))
            for kv in result.kvs
        )

    def rollback(self, app: str, layer: str, key: str, *, to: int) -> int:
        """Restore the value the `to`-th recorded change made live, as a new change.

        Never rewrites history: the restore is itself a change, with its own
        entry naming the actor who performed it. Rolling back twice therefore
        leaves two entries and no ambiguity, which is the same discipline as
        `prompt-registry`'s published versions being append-only.
        """
        entries = {entry.sequence: entry for entry in self.history(app, layer, key)}
        entry = entries.get(to)
        if entry is None:
            raise KeyError(
                f"no history entry {to} for {config_key(app, layer, key)}; "
                f"recorded sequences are {sorted(entries) or '(none)'}"
            )
        if entry.op == "unset":
            live = self.current(app, layer, key)
            if live is None:
                raise KeyError(
                    f"{config_key(app, layer, key)} is already absent, which is "
                    f"what the entry at {to} recorded"
                )
            return self.unset(app, layer, key, expect=live.mod_revision)
        return self.set(app, layer, key, entry.value)

    # -- internals ---------------------------------------------------------

    def _next_sequence(self, app: str, layer: str, key: str) -> int:
        """The next history slot for a key: one past the highest recorded.

        One extra range read per write, bounded to a single key by
        `limit=1` and `DESCEND`. It does not need to be atomic with the write:
        the transaction asserts the slot is empty and compare-and-swaps the
        value, so a stale count can only cause a refused write — never a lost
        or overwritten entry.
        """
        latest = self._etcd.range(
            history_prefix(app, layer, key),
            prefix=True,
            keys_only=True,
            limit=1,
            sort_order="DESCEND",
            sort_target="KEY",
        )
        if not latest.kvs:
            return 1
        return history_sequence(latest.kvs[0].key) + 1

    def _record(
        self, op: str, value: Any, previous: Any, replaces: int, sequence: int
    ) -> bytes:
        return _json(
            {
                "op": op,
                "value": value,
                "previous": previous,
                "actor": self._actor,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "replaces": replaces,
                "sequence": sequence,
            }
        )


def compact_to_latest(etcd: Etcd, *, keep: int = 1000) -> int:
    """Compact MVCC history, keeping the last `keep` revisions.

    Exposed because it is not optional. etcd's own `--auto-compaction-retention`
    should be doing this; this is the manual equivalent for a cluster started
    without it, and the number it returns is the revision compacted to.

    Safe with respect to *this* store's history because that history is ordinary
    keys, which compaction does not touch. It only discards the ability to read
    old revisions with `range(revision=...)`, which nothing here relies on.
    """
    revision = etcd.status()
    current = int(revision.get("header", {}).get("revision", 0))
    target = max(current - keep, 1)
    etcd.compact(target)
    return target
