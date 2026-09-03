"""How the console reaches etcd. One place, so no view builds its own client."""

from __future__ import annotations

import json
import os
from typing import Any

import streamlit as st

from configstore.admin import ConfigAdmin
from configstore.etcd import Etcd

DEFAULT_URL = "http://127.0.0.1:2379"


def etcd_url() -> str:
    """Read per call rather than captured at import, so the cache below is
    keyed by the address and a changed environment actually takes effect."""
    return os.getenv("CONFIGSTORE_ETCD_URL", DEFAULT_URL)


def actor() -> str:
    """Who the console attributes writes to.

    An environment variable, because this console is a single-operator tool
    behind a VPN. Putting a real identity on every change is the whole point of
    the field, so the moment more than one person uses it this needs to come
    from an authenticated session — the same OIDC identity `step ssh login`
    already issues against — rather than from a variable anyone can set.
    """
    return os.getenv("CONFIGSTORE_ACTOR") or os.getenv("USER") or "console"


@st.cache_resource
def _connect(url: str) -> Etcd:
    return Etcd(url)


def etcd() -> Etcd:
    return _connect(etcd_url())


def admin() -> ConfigAdmin:
    return ConfigAdmin(etcd(), actor=actor())


def display(value: Any) -> str:
    """Render a config value for a table cell.

    Everything goes through JSON so a column holds one type. A dataframe column
    mixing a bool, an int and a string is exactly what configuration looks like,
    and Arrow — which is what `st.dataframe` serialises through — refuses to
    infer a type for it and raises. Stringifying also keeps `true` visibly
    different from `"true"`, which matters when the difference is a bug someone
    is hunting.
    """
    return json.dumps(value)
