from __future__ import annotations

from typing import Any

import pytest

from configstore.schema import (
    KeySpec,
    KeyType,
    Schema,
    SchemaError,
    UnknownKey,
    ValidationError,
)


def test_a_typo_is_not_configuration(schema: Schema) -> None:
    with pytest.raises(UnknownKey):
        schema.coerce("retreival.top_k", 8)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("retrieval.top_k", "eight"),
        ("retrieval.top_k", 1.5),
        ("chat.model", 3),
        ("judge.enabled", "true"),
    ],
)
def test_rejects_values_of_the_wrong_type(schema: Schema, key: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        schema.coerce(key, value)


def test_booleans_and_integers_do_not_bleed_into_each_other(schema: Schema) -> None:
    # `True` is an `int` in Python, so an unguarded int key would accept it and
    # an unguarded bool key would accept 1. A feature flag is the last place
    # that should be loose.
    with pytest.raises(ValidationError):
        schema.coerce("retrieval.top_k", True)
    with pytest.raises(ValidationError):
        schema.coerce("judge.enabled", 1)


def test_secret_keys_take_a_reference_not_a_literal(schema: Schema) -> None:
    with pytest.raises(ValidationError):
        schema.coerce("api.key", "sk-live-9f3ac2")
    assert schema.coerce("api.key", "infisical://apps/chatbot/prod#KEY")


def test_a_required_key_with_a_default_is_a_contradiction() -> None:
    with pytest.raises(SchemaError):
        KeySpec("k", KeyType.INT, default=3, required=True)


def test_a_bad_default_fails_when_the_schema_is_written() -> None:
    with pytest.raises(ValidationError):
        KeySpec("k", KeyType.INT, default="three")


def test_round_trips_through_storage(schema: Schema) -> None:
    assert Schema.from_bytes(schema.to_bytes()) == schema
