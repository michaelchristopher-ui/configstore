from __future__ import annotations

import pytest

from configstore.layout import (
    InvalidName,
    check_app,
    check_env,
    check_key,
    config_key,
    history_key,
    history_sequence,
    parse_config_key,
)


@pytest.mark.parametrize("name", ["chatbot", "a", "a-b-c", "x9"])
def test_accepts_ordinary_app_names(name: str) -> None:
    assert check_app(name) == name


@pytest.mark.parametrize("name", ["Chatbot", "-x", "x-", "a_b", "a/b", "", "a" * 65])
def test_rejects_names_that_would_break_the_layout(name: str) -> None:
    with pytest.raises(InvalidName):
        check_app(name)


def test_base_is_not_available_as_an_environment() -> None:
    with pytest.raises(InvalidName):
        check_env("base")


def test_keys_may_not_contain_a_path_separator() -> None:
    # The layout depends on this: a key with a slash in it would make
    # parse_config_key ambiguous between a deep key and another layer.
    with pytest.raises(InvalidName):
        check_key("retrieval/top_k")


def test_config_key_round_trips() -> None:
    key = config_key("chatbot", "prod", "retrieval.top_k")
    parsed = parse_config_key(key)
    assert (parsed.app, parsed.layer, parsed.key) == (
        "chatbot",
        "prod",
        "retrieval.top_k",
    )


def test_parse_rejects_keys_from_other_prefixes() -> None:
    with pytest.raises(InvalidName):
        parse_config_key(history_key("chatbot", "prod", "k", 5))


def test_history_keys_sort_in_change_order() -> None:
    # Zero-padding is what makes a plain range read chronological: unpadded,
    # "10" sorts before "9" bytewise and history lists itself out of order.
    keys = [history_key("a", "base", "k", n) for n in (1, 2, 9, 10, 100)]
    assert keys == sorted(keys)
    assert [history_sequence(k) for k in sorted(keys)] == [1, 2, 9, 10, 100]


def test_history_sequences_start_at_one() -> None:
    # So an empty history is distinguishable from a first entry with no
    # sentinel value, and slot 0 is never occupied.
    with pytest.raises(InvalidName):
        history_key("a", "base", "k", 0)
