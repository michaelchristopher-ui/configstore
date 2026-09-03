"""Fixtures. The etcd-backed ones skip rather than fail when there is no etcd.

`make etcd-up` starts one; CI sets `CONFIGSTORE_ETCD_URL`. Tests that need a
real store are marked `etcd` so `pytest -m 'not etcd'` is a complete unit run.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from configstore.admin import ConfigAdmin
from configstore.etcd import Etcd, EtcdUnavailable
from configstore.schema import KeySpec, KeyType, Schema

ETCD_URL = os.getenv("CONFIGSTORE_ETCD_URL", "http://127.0.0.1:2379")


@pytest.fixture(scope="session")
def etcd_url() -> str:
    probe = Etcd(ETCD_URL, timeout=1.0)
    try:
        probe.status()
    except EtcdUnavailable:
        pytest.skip(f"no etcd at {ETCD_URL} (start one with `make etcd-up`)")
    return ETCD_URL


@pytest.fixture
def etcd(etcd_url: str) -> Iterator[Etcd]:
    client = Etcd(etcd_url)
    yield client
    client.close()


@pytest.fixture
def app() -> str:
    """A fresh app name per test, so nothing has to be cleaned up.

    Deliberately additive rather than tearing down: this store's whole posture
    is that it does not delete things, and a test suite that deletes keys is one
    `--app` typo away from clearing someone's configuration.
    """
    return f"t{uuid.uuid4().hex[:12]}"


@pytest.fixture
def schema(app: str) -> Schema:
    return Schema.of(
        app,
        [
            KeySpec("retrieval.top_k", KeyType.INT, default=8),
            KeySpec("retrieval.rrf_k", KeyType.INT, default=60),
            KeySpec("chat.model", KeyType.STRING, default="qwen/qwen3-vl-8b"),
            KeySpec("judge.enabled", KeyType.BOOL, default=False),
            KeySpec("api.key", KeyType.STRING, secret_ref=True),
        ],
    )


@pytest.fixture
def admin(etcd: Etcd, schema: Schema) -> ConfigAdmin:
    writer = ConfigAdmin(etcd, actor="tests@example.com")
    writer.publish_schema(schema)
    return writer
