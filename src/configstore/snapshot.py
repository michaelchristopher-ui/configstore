"""A resolved configuration, frozen at one revision.

Resolution is a pure function of (raw keys, schema, environment), which is the
property the whole client depends on: the watch loop keeps a dict of raw keys up
to date and rebuilds a snapshot from it, so there is no incremental-merge logic
to get wrong when an environment override is *deleted* and the base value has to
come back.

Precedence, lowest first:

    schema default   what the application would do with no configuration
    base layer       what an operator chose for every environment
    <env> layer      what this environment overrides

`revision` stamps the whole thing. Two applications that resolved at the same
revision hold byte-identical configuration, and that is checkable rather than
hoped for — which is the answer to "is prod actually running what the console
shows?"
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .layout import BASE_LAYER, parse_config_key
from .schema import Schema, UnknownKey, ValidationError

# What `sources` reports for a value no layer supplied.
FROM_DEFAULT = "default"


class MissingRequired(RuntimeError):
    """A required key no layer supplied — an app that must not start."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Configuration as of one revision. Immutable, and cheap to hold."""

    app: str
    env: str
    revision: int
    values: Mapping[str, Any]
    # Which layer each value came from, for the console and for debugging the
    # question that actually gets asked: not "what is top_k" but "why is it 5".
    sources: Mapping[str, str]
    # Keys the store holds that the schema does not declare. Never dropped and
    # never deleted — a key that stopped being declared is usually a rename in
    # flight or a rollback waiting to happen, and this store does not throw away
    # values it does not currently understand. Reported so the console can show
    # them; excluded from `values` so an application cannot read one by accident.
    unknown: tuple[str, ...] = ()
    # Values that failed their declared type and were skipped in favour of the
    # layer below. Carried rather than raised — see `resolve`.
    invalid: tuple[str, ...] = ()
    # True when this came off the local cache because etcd could not be reached.
    # The application still starts; it just knows it is running on last-known
    # state, which is the trade a config *service* in the request path cannot
    # offer at all.
    stale: bool = False
    fetched_at: float = field(default_factory=time.time)

    def get(self, key: str, fallback: Any = None) -> Any:
        return self.values.get(key, fallback)

    def require(self, key: str) -> Any:
        try:
            return self.values[key]
        except KeyError:
            raise MissingRequired(
                f"{key!r} has no value in {self.app}/{self.env} at revision "
                f"{self.revision} and no schema default"
            ) from None

    def overridden(self) -> tuple[str, ...]:
        """Keys this environment overrides rather than inherits."""
        return tuple(
            sorted(
                k
                for k, src in self.sources.items()
                if src not in (FROM_DEFAULT, BASE_LAYER)
            )
        )

    # -- the on-disk cache -------------------------------------------------

    def to_cache(self, raw: Mapping[str, Any]) -> bytes:
        """Serialise the *raw* keys, not the resolved values.

        Raw because the schema is compiled into the application: caching
        resolved values would freeze the defaults of whatever version wrote the
        cache, so upgrading an application would silently keep serving the old
        default until someone changed the key. Raw keys plus the new code's
        schema resolve to the right answer.
        """
        return json.dumps(
            {
                "app": self.app,
                "env": self.env,
                "revision": self.revision,
                "saved_at": time.time(),
                "raw": dict(raw),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


def resolve(
    *,
    app: str,
    env: str,
    revision: int,
    raw: Mapping[str, Any],
    schema: Schema,
    stale: bool = False,
) -> Snapshot:
    """Merge raw etcd keys into a snapshot, applying schema types and defaults.

    `raw` is keyed by full etcd key, exactly as a range over the app prefix
    returns it, so the same function serves a cold read, a watch update and the
    disk cache without any of them needing to know about the others.

    A value that fails its declared type is *dropped* rather than fatal, and the
    layer below it wins. That is the deliberate choice: this runs at application
    start and on every config change, so raising here would let one bad write in
    the console take down every running replica at once. Writes are validated by
    `ConfigAdmin`, which is where a bad value can still be refused to a human;
    by the time it is in the store, serving the previous good value beats
    serving nothing. Rejections are reported in `invalid`.
    """
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    unknown: set[str] = set()
    invalid: list[str] = []

    for name, default in schema.defaults().items():
        values[name] = default
        sources[name] = FROM_DEFAULT

    # base first, then the environment, so the environment's writes land last
    # and win. Anything else in the app's prefix is another environment's and is
    # not this snapshot's business.
    for layer in (BASE_LAYER, env):
        for etcd_key, value in raw.items():
            path = parse_config_key(etcd_key)
            if path.layer != layer:
                continue
            try:
                values[path.key] = schema.coerce(path.key, value)
            except UnknownKey:
                unknown.add(path.key)
                continue
            except ValidationError as exc:
                invalid.append(str(exc))
                continue
            sources[path.key] = layer

    missing = [k for k in schema.required() if k not in values]
    if missing:
        raise MissingRequired(
            f"{app}/{env} at revision {revision} is missing required "
            f"{', '.join(missing)} — no layer supplies it and it has no default"
        )

    return Snapshot(
        app=app,
        env=env,
        revision=revision,
        values=values,
        sources=sources,
        unknown=tuple(sorted(unknown)),
        invalid=tuple(invalid),
        stale=stale,
    )
