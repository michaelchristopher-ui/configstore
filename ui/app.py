"""The Streamlit console: a lightweight frontend over the store, for writes.

It is deliberately not in anybody's read path. Applications read configuration
from a local snapshot kept current by a watch (`configstore.client`), so this
process can be down, slow, or not deployed at all without any application
noticing — which is why it can afford to be this simple.

Run it with ``make ui``, or:

    uv run streamlit run ui/app.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

# Streamlit puts this file's directory on sys.path, not the repository root, so
# the `ui.` package would not resolve when launched as `streamlit run ui/app.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402

from configstore.etcd import EtcdUnavailable  # noqa: E402
from ui.store import actor, etcd, etcd_url  # noqa: E402
from ui.views import history as history_view  # noqa: E402
from ui.views import resolve as resolve_view  # noqa: E402
from ui.views import values as values_view  # noqa: E402


def main() -> None:
    st.set_page_config(page_title="configstore", page_icon="🗂", layout="wide")

    with st.sidebar:
        st.title("configstore")
        st.caption(f"`{etcd_url()}`")
        try:
            status = etcd().status()
            size = int(status.get("dbSize", 0))
            in_use = int(status.get("dbSizeInUse", 0))
            st.metric("Revision", status.get("header", {}).get("revision", "?"))
            st.caption(
                f"etcd {status.get('version')} — {size / 1_048_576:.1f} MiB on disk, "
                f"{in_use / 1_048_576:.1f} MiB in use"
            )
            if size and in_use / size < 0.5:
                st.caption(
                    "Over half the file is free space; `etcdutl defrag` returns it."
                )
            for alarm in status.get("errors", ()):
                st.error(f"Cluster alarm: {alarm}")
        except EtcdUnavailable as down:
            st.error("etcd is unreachable", icon="⚠️")
            st.caption(str(down)[:200])
            st.caption(
                "Running applications are unaffected — they serve from their own "
                "snapshots. Only editing is blocked."
            )
        st.caption(f"Writing as **{actor()}**")

    def guarded(render: Callable[[], None]) -> None:
        """Render a view, reporting an unreachable store rather than crashing.

        Every view reads before it draws, so an etcd that is down would
        otherwise surface as a Streamlit traceback — which reads like the
        *store* is broken. It is not: applications serve from their own
        snapshots and are unaffected. Only this console is.
        """
        try:
            render()
        except EtcdUnavailable as down:
            st.error("etcd is unreachable, so configuration cannot be read or edited.")
            st.code(str(down)[:400])
            st.info(
                "Applications already running are unaffected — each holds its own "
                "snapshot and keeps serving it. Nothing here is lost."
            )

    # Named functions with explicit url_paths: st.navigation derives a page's
    # URL from the callable's name, so two lambdas would collide on "<lambda>".
    def resolved_page() -> None:
        guarded(resolve_view.render)

    def values_page() -> None:
        guarded(values_view.render)

    def history_page() -> None:
        guarded(history_view.render)

    page = st.navigation(
        [
            st.Page(
                resolved_page,
                title="Resolved",
                icon="🔍",
                url_path="resolved",
                default=True,
            ),
            st.Page(values_page, title="Values", icon="✏️", url_path="values"),
            st.Page(history_page, title="History", icon="🕓", url_path="history"),
        ]
    )
    page.run()


main()
