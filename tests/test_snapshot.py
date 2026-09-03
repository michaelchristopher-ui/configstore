from __future__ import annotations

import pytest

from configstore.layout import config_key
from configstore.schema import KeySpec, KeyType, Schema
from configstore.snapshot import FROM_DEFAULT, MissingRequired, Snapshot, resolve


def _resolve(
    app: str, schema: Schema, raw: dict[str, object], env: str = "prod"
) -> Snapshot:
    return resolve(app=app, env=env, revision=7, raw=raw, schema=schema)


def test_precedence_is_default_then_base_then_environment(
    app: str, schema: Schema
) -> None:
    snap = _resolve(
        app,
        schema,
        {
            config_key(app, "base", "retrieval.top_k"): 10,
            config_key(app, "prod", "retrieval.top_k"): 12,
        },
    )
    assert snap.get("retrieval.top_k") == 12
    assert snap.sources["retrieval.top_k"] == "prod"
    # untouched by any layer, so the schema default stands
    assert snap.get("retrieval.rrf_k") == 60
    assert snap.sources["retrieval.rrf_k"] == FROM_DEFAULT


def test_another_environments_keys_are_not_this_snapshots_business(
    app: str, schema: Schema
) -> None:
    snap = _resolve(
        app,
        schema,
        {
            config_key(app, "base", "retrieval.top_k"): 10,
            config_key(app, "staging", "retrieval.top_k"): 999,
        },
    )
    assert snap.get("retrieval.top_k") == 10


def test_a_bad_value_in_the_store_falls_back_instead_of_failing(
    app: str, schema: Schema
) -> None:
    # Resolution runs on every config change in every replica. Raising here
    # would let one bad write take down a fleet, so the layer below wins and
    # the rejection is reported.
    snap = _resolve(
        app,
        schema,
        {
            config_key(app, "base", "retrieval.top_k"): 10,
            config_key(app, "prod", "retrieval.top_k"): "twelve",
        },
    )
    assert snap.get("retrieval.top_k") == 10
    assert snap.invalid and "retrieval.top_k" in snap.invalid[0]


def test_undeclared_keys_are_reported_but_not_readable(
    app: str, schema: Schema
) -> None:
    snap = _resolve(app, schema, {config_key(app, "prod", "legacy.flag"): True})
    assert snap.unknown == ("legacy.flag",)
    assert snap.get("legacy.flag") is None


def test_a_missing_required_key_stops_the_application(app: str) -> None:
    schema = Schema.of(app, [KeySpec("chat.model", KeyType.STRING, required=True)])
    with pytest.raises(MissingRequired):
        _resolve(app, schema, {})
