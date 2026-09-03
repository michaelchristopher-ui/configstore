"""The console renders and does not blow up. Uses Streamlit's own harness.

Not a substitute for looking at it, but it catches the failure that linting and
type checking both miss: a page that raises on load.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.etcd

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

ROOT = str(Path(__file__).resolve().parent.parent)
APP = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")


@pytest.fixture(autouse=True)
def console_env(etcd_url: str) -> None:
    # Read by `ui.store` at import time, so it has to be set before the app runs.
    os.environ["CONFIGSTORE_ETCD_URL"] = etcd_url
    os.environ["CONFIGSTORE_ACTOR"] = "console-tests"


def test_the_console_loads_without_raising() -> None:
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert "configstore" in [title.value for title in app.sidebar.title]


@pytest.mark.parametrize("view", ["resolve", "values", "history"])
def test_every_view_renders(view: str, admin: object, app: str) -> None:
    # Navigation is built from callables rather than page files, so there is no
    # page file to switch to. Each view is driven through a one-line script,
    # which is what `st.Page` ends up doing anyway. `AppTest.from_function`
    # cannot be used here: it runs a function's source without its module's
    # imports, so `st` would be undefined.
    rendered = AppTest.from_string(
        f"import sys; sys.path.insert(0, {ROOT!r})\n"
        f"from ui.views import {view}\n"
        f"{view}.render()\n",
        default_timeout=30,
    ).run()
    assert not rendered.exception, f"{view} raised: {rendered.exception}"


def test_it_reports_an_unreachable_etcd_instead_of_crashing() -> None:
    # The console being unable to reach etcd must not look like a crash, because
    # it does not mean applications are affected.
    os.environ["CONFIGSTORE_ETCD_URL"] = "http://127.0.0.1:1"
    import streamlit as st

    st.cache_resource.clear()
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert any("unreachable" in error.value for error in app.sidebar.error)
