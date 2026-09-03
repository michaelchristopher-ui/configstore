"""The key layout. In etcd this *is* the schema.

etcd offers exactly one way to find a key you cannot name: scan a prefix. There
are no secondary indexes, no joins and no `WHERE`, so every question the store
must answer has to fall out of the shape of the keyspace. Four prefixes:

    /cfg/v1/<app>/<layer>/<key>              the live value       (JSON)
    /meta/v1/<app>/schema                    declared keys        (JSON)
    /hist/v1/<app>/<layer>/<key>/<seq>       append-only history  (JSON)

**Why `<app>` sits above `<layer>`.** The one read on the hot path is "resolve
every layer of one app", so those keys must share a prefix — one range read,
one round trip, and atomic by construction because a single etcd range is
served from one MVCC snapshot. Putting the environment first would have made
resolution two reads that need a revision pinned across them to avoid tearing.
The layout is chosen by what the read path needs, not by what reads tidily.

**Why keys may not contain `/`.** A dotted key (`retrieval.top_k`) maps to
exactly one etcd key and back again. Allowing `/` would let `a/b` and the
nesting separator collide, so a range over one app could not tell a deep key
from another layer, and `parse_config_key` could not be a function.

**Why history is keyed by a per-key sequence rather than by a revision.** A
change cannot know the revision it will land at — etcd assigns that on commit —
so history keyed by its own revision cannot be written in the transaction that
makes the change, and an audit trail that can be missing entries is not one.
The obvious fix, naming the slot after the revision the change *supersedes*
(knowable in advance), has a hole: a creation supersedes nothing, so every
creation claims the same slot, and recreating a key that was previously removed
silently overwrites the original creation's entry. A per-key sequence has no
such case. Uniqueness does not depend on the counter being read atomically —
the transaction also asserts that the slot is empty, and the compare-and-swap on
the value serialises racing writers anyway — so a stale read of the counter
cannot produce a lost entry, only a refused write.

**Why sequences are zero-padded.** etcd orders keys bytewise, so `.../9` sorts
after `.../10` and an unpadded history prefix would list itself out of order.
Padded to 20 digits — one more than int64's 19 — a plain range read comes back
in change order, and `limit` on it means "the N oldest" rather than "N
arbitrary".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CFG_PREFIX = "/cfg/v1/"
META_PREFIX = "/meta/v1/"
HIST_PREFIX = "/hist/v1/"

# The layer every app has, under every environment, holding the values an
# environment does not override. Reserved as a layer name for that reason: an
# environment called `base` would be indistinguishable from it.
BASE_LAYER = "base"

_REV_WIDTH = 20

_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_KEY = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")

MAX_NAME = 64
MAX_KEY = 128


class InvalidName(ValueError):
    """A name that would break the layout's guarantees, refused at the edge."""


def check_app(app: str) -> str:
    if not _NAME.match(app) or len(app) > MAX_NAME:
        raise InvalidName(
            f"app name {app!r} must be lowercase alphanumeric with inner "
            f"hyphens, at most {MAX_NAME} characters"
        )
    return app


def check_layer(layer: str) -> str:
    if not _NAME.match(layer) or len(layer) > MAX_NAME:
        raise InvalidName(
            f"layer name {layer!r} must be lowercase alphanumeric with inner "
            f"hyphens, at most {MAX_NAME} characters"
        )
    return layer


def check_env(env: str) -> str:
    """An environment name — a layer that is not the base layer.

    Separate from `check_layer` so the reservation is enforced where an
    environment is *named by a caller*, and not where the resolver names the
    base layer itself.
    """
    if env == BASE_LAYER:
        raise InvalidName(
            f"{BASE_LAYER!r} is the reserved name of the layer every "
            "environment falls back to, so it cannot also be an environment"
        )
    return check_layer(env)


def check_key(key: str) -> str:
    if not _KEY.match(key) or len(key) > MAX_KEY:
        raise InvalidName(
            f"config key {key!r} must be dot-separated lowercase segments of "
            f"[a-z0-9_], at most {MAX_KEY} characters, and may not contain '/'"
        )
    return key


def app_prefix(app: str) -> str:
    """Every layer of one app — the prefix the read path ranges over."""
    return f"{CFG_PREFIX}{check_app(app)}/"


def layer_prefix(app: str, layer: str) -> str:
    return f"{app_prefix(app)}{check_layer(layer)}/"


def config_key(app: str, layer: str, key: str) -> str:
    return f"{layer_prefix(app, layer)}{check_key(key)}"


def schema_key(app: str) -> str:
    return f"{META_PREFIX}{check_app(app)}/schema"


def history_prefix(app: str, layer: str, key: str) -> str:
    return f"{HIST_PREFIX}{check_app(app)}/{check_layer(layer)}/{check_key(key)}/"


def history_key(app: str, layer: str, key: str, sequence: int) -> str:
    """The history slot for the `sequence`-th recorded change to a key.

    Sequences start at 1, so slot 0 is never written and an empty history is
    distinguishable from a first entry without a sentinel.
    """
    if sequence < 1:
        raise InvalidName(f"history sequence {sequence} must be 1 or greater")
    return f"{history_prefix(app, layer, key)}{sequence:0{_REV_WIDTH}d}"


@dataclass(frozen=True, slots=True)
class ConfigPath:
    """A parsed `/cfg/v1/...` key."""

    app: str
    layer: str
    key: str


def parse_config_key(etcd_key: str) -> ConfigPath:
    """Split a live-config key back into its parts.

    Total for anything this layout wrote, which is what lets the watch loop
    route an event without consulting anything else. Raises for a key from
    another prefix rather than guessing — a `/hist/` key arriving here would
    mean the watch was registered on the wrong prefix, and silently coercing it
    would turn that into wrong config instead of a stack trace.
    """
    if not etcd_key.startswith(CFG_PREFIX):
        raise InvalidName(f"{etcd_key!r} is not under {CFG_PREFIX}")
    rest = etcd_key[len(CFG_PREFIX) :]
    parts = rest.split("/")
    if len(parts) != 3 or not all(parts):
        raise InvalidName(
            f"{etcd_key!r} does not have the shape {CFG_PREFIX}<app>/<layer>/<key>"
        )
    return ConfigPath(app=parts[0], layer=parts[1], key=parts[2])


def history_sequence(etcd_key: str) -> int:
    """Which change a history entry records, from its zero-padded final segment."""
    return int(etcd_key.rsplit("/", 1)[1])
