"""Who changed what, and putting it back.

History here is ordinary etcd keys rather than MVCC revisions, which is why this
page still works after compaction — see `configstore.admin`.
"""

from __future__ import annotations

import streamlit as st

from configstore.admin import Conflict
from ui.store import admin, display


def render() -> None:
    st.header("History")
    writer = admin()
    apps = writer.apps()
    if not apps:
        st.info("No applications have any configuration yet.")
        return

    app = st.selectbox("Application", apps)
    layers = writer.layers(app)
    layer = st.selectbox("Layer", layers)
    keys = sorted(writer.read_layer(app, layer))
    typed = st.text_input(
        "…or a key that is no longer set", placeholder="retrieval.top_k"
    )
    key = typed or (st.selectbox("Key", keys) if keys else "")
    if not key:
        st.info("This layer holds no keys.")
        return

    entries = writer.history(app, layer, key)
    if not entries:
        st.info(f"No recorded changes to `{key}` in `{layer}`.")
        return

    st.dataframe(
        [
            {
                "#": entry.sequence,
                "when": entry.at,
                "who": entry.actor,
                "what": entry.op,
                "from": display(entry.previous),
                "to": display(entry.value),
            }
            for entry in entries
        ],
        hide_index=True,
        width="stretch",
    )

    st.subheader("Restore")
    st.caption(
        "A restore is appended as a new change with its own author — history is "
        "never rewritten, so rolling back twice leaves two entries and no doubt "
        "about what happened."
    )
    choice = st.selectbox(
        "Restore the value from change…",
        entries,
        format_func=lambda e: (
            f"#{e.sequence} — {e.op} to {e.value!r} by {e.actor} at {e.at}"
        ),
    )
    if st.button("Restore", type="primary"):
        try:
            revision = writer.rollback(app, layer, key, to=choice.sequence)
            st.success(f"Restored at revision {revision}.")
        except (Conflict, KeyError) as problem:
            st.error(str(problem))
