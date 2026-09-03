"""Editing a layer.

Two things this page does that a form over a database table usually does not:

- **It sends the revision it displayed.** Every write carries `expect=` the
  `mod_revision` that was on screen, so if someone else changed the key while
  the form sat open, the write is refused and the operator is shown the value
  they would have overwritten. Nothing is lost silently.
- **It saves the whole form as one transaction.** Edits to several keys go
  through `apply`, so running applications see one atomic change rather than a
  sequence of half-states.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from configstore.admin import Conflict
from configstore.layout import BASE_LAYER, InvalidName, check_layer
from configstore.schema import KeySpec, KeyType, Schema, ValidationError
from ui.store import admin


def _editor(spec: KeySpec, current: Any, key: str) -> Any:
    """The right widget for a declared type, defaulting to what is live."""
    if spec.type is KeyType.BOOL:
        return st.checkbox(
            spec.name, value=bool(current), key=key, help=spec.description
        )
    if spec.type is KeyType.INT:
        return int(
            st.number_input(
                spec.name,
                value=int(current or 0),
                step=1,
                key=key,
                help=spec.description,
            )
        )
    if spec.type is KeyType.FLOAT:
        return float(
            st.number_input(
                spec.name, value=float(current or 0.0), key=key, help=spec.description
            )
        )
    if spec.secret_ref:
        return st.text_input(
            spec.name,
            value=str(current or ""),
            key=key,
            help="A reference, not the secret itself — etcd stores this in plaintext.",
            placeholder="infisical://apps/<app>/<env>#FIELD",
        )
    if spec.type is KeyType.JSON:
        return st.text_area(
            spec.name, value=str(current or ""), key=key, help=spec.description
        )
    return st.text_input(
        spec.name, value=str(current or ""), key=key, help=spec.description
    )


def render() -> None:
    st.header("Values")
    writer = admin()
    apps = writer.apps()

    with st.sidebar:
        app = (
            st.selectbox("Application", apps)
            if apps
            else st.text_input("New application")
        )
        known = list(writer.layers(app)) if app else []
        options = sorted({BASE_LAYER, *known})
        layer = st.selectbox("Layer", options) if options else BASE_LAYER
        new_layer = st.text_input("…or a new environment", placeholder="prod")
        if new_layer:
            try:
                layer = check_layer(new_layer)
            except InvalidName as bad:
                st.error(str(bad))
                return
    if not app:
        st.info("Name an application to begin.")
        return

    schema: Schema = writer.read_schema(app)
    if not schema.keys:
        st.warning(
            f"No schema is published for `{app}`, so there is nothing to edit and "
            "writes would be unchecked. Publish one with "
            "`configstore push-schema --file <schema.json>`."
        )
        return

    live = writer.read_layer(app, layer)
    st.caption(
        f"`{app}` / `{layer}` — {len(live)} of {len(schema.keys)} declared keys are "
        f"set in this layer. Anything unset falls through to "
        f"{'the schema default' if layer == BASE_LAYER else f'`{BASE_LAYER}`'}."
    )

    with st.form("values"):
        edits: dict[str, Any] = {}
        include: dict[str, bool] = {}
        for name in sorted(schema.keys):
            spec = schema.keys[name]
            existing = live.get(name)
            columns = st.columns([1, 4])
            with columns[0]:
                include[name] = st.checkbox(
                    "set",
                    value=existing is not None,
                    key=f"on-{name}",
                    label_visibility="visible",
                )
            with columns[1]:
                current = existing.json() if existing else spec.default
                edits[name] = _editor(spec, current, key=f"v-{name}")
        submitted = st.form_submit_button("Save as one transaction", type="primary")

    if not submitted:
        return

    # `expect` is what was on screen. A key someone else changed in the meantime
    # is refused rather than overwritten.
    expect = {name: kv.mod_revision for name, kv in live.items()}
    wanted = {name: value for name, value in edits.items() if include[name]}
    removing = [name for name in live if not include.get(name)]

    try:
        if wanted:
            revision = writer.apply(app, layer, wanted, expect=expect)
            st.success(f"{len(wanted)} keys written together at revision {revision}.")
        for name in removing:
            writer.unset(app, layer, name, expect=expect[name])
            st.success(f"`{name}` removed; it now falls through to the layer below.")
        if not wanted and not removing:
            st.info("Nothing changed.")
    except Conflict as conflict:
        st.error(str(conflict))
        if conflict.current is not None:
            st.json({"value someone else wrote": conflict.current.json()})
        st.warning("Nothing was written. Reload to see the current values.")
    except ValidationError as invalid:
        st.error(f"Refused: {invalid}")
