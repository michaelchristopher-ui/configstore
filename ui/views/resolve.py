"""What an application would actually see, and which layer each value came from.

The page that exists because "what is prod running" and "what did someone type
into the console" are different questions. This answers the first one by running
the same resolver an application runs.
"""

from __future__ import annotations

import streamlit as st

from configstore.client import ConfigClient
from configstore.layout import BASE_LAYER
from configstore.snapshot import FROM_DEFAULT, MissingRequired
from ui.store import admin, display, etcd


def render() -> None:
    st.header("Resolved")
    writer = admin()
    apps = writer.apps()
    if not apps:
        st.info("No applications have any configuration yet.")
        return

    app = st.selectbox("Application", apps)
    layers = [layer for layer in writer.layers(app) if layer != BASE_LAYER]
    if not layers:
        st.warning(
            f"`{app}` has only a `{BASE_LAYER}` layer, so there is no environment "
            "to resolve. Add a value under an environment name to create one."
        )
        return
    env = st.selectbox("Environment", layers)

    schema = writer.read_schema(app)
    client = ConfigClient(etcd(), app=app, env=env, schema=schema, watch=False)
    try:
        snapshot = client.start()
    except MissingRequired as missing:
        st.error(f"This application would refuse to start: {missing}")
        return

    st.caption(
        f"Revision {snapshot.revision} — an application reading this app and "
        "environment right now holds exactly these values."
    )
    st.dataframe(
        [
            {
                "key": key,
                "value": display(snapshot.values[key]),
                "from": snapshot.sources[key],
                "overridden here": snapshot.sources[key]
                not in (FROM_DEFAULT, BASE_LAYER),
            }
            for key in sorted(snapshot.values)
        ],
        hide_index=True,
        width="stretch",
    )

    if snapshot.unknown:
        st.warning(
            "In the store but not declared by the schema, so no application can "
            f"read them: {', '.join(snapshot.unknown)}. Nothing has been deleted — "
            "either declare them or leave them."
        )
    for problem in snapshot.invalid:
        st.error(f"Rejected by the schema, so the layer below is being used: {problem}")
