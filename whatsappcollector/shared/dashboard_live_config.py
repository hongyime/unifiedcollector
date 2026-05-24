"""
shared/dashboard_live_config.py — Reusable Streamlit Live Config panel.

Renders a per-service live configuration panel that lets operators inspect
and mutate tunable parameters through the Streamlit UI.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import streamlit as st

from shared.live_config import ConfigOverlay, ConfigValidationError

if TYPE_CHECKING:
    from shared.live_config import ParameterMeta


def _select_widget(
    meta: "ParameterMeta",
    current_value: object,
    env_default: object,
) -> object:
    """Render the appropriate Streamlit widget for *meta* and return the new value.

    Widget dispatch rules (per ParameterMeta):
    - bool                              → st.toggle
    - int/float with min + max defined  → st.slider
    - str with options list             → st.selectbox
    - str free-form (no options)        → st.text_input
    - float without min or max          → st.number_input
    """
    label = meta.key
    is_overridden = current_value != env_default

    # Visual indicator for overridden values (Req 7.7)
    if is_overridden:
        display_label = f"★ {label}"
    else:
        display_label = label

    if meta.python_type is bool:
        # Req 7.2
        return st.toggle(display_label, value=bool(current_value))

    if meta.python_type in (int, float) and meta.min_value is not None and meta.max_value is not None:
        # Req 7.3
        return st.slider(
            display_label,
            min_value=meta.min_value if meta.python_type is float else int(meta.min_value),
            max_value=meta.max_value if meta.python_type is float else int(meta.max_value),
            value=current_value,
        )

    if meta.python_type is str and meta.multi_select:
        # NEW: multi-select string (sub-tasks 2.1–2.5)
        option_pool: list[str] = (
            meta.options if meta.options is not None
            else meta.known_values if meta.known_values is not None
            else []
        )
        current_list = [
            token.strip()
            for token in str(current_value).split(",")
            if token.strip()
        ]
        # Merge any current tokens not already in option_pool (preserves order)
        merged_options = list(option_pool) + [t for t in current_list if t not in option_pool]
        selected: list[str] = st.multiselect(
            display_label,
            options=merged_options,
            default=current_list,
        )
        return ",".join(selected)

    if meta.python_type is str and meta.options:
        # Req 7.4
        options = meta.options
        try:
            idx = options.index(str(current_value))
        except ValueError:
            idx = 0
        return st.selectbox(display_label, options=options, index=idx)

    if meta.python_type is str:
        # Req 7.5
        return st.text_input(display_label, value=str(current_value))

    if meta.python_type is float:
        # Req 7.6 — float without min/max
        return st.number_input(display_label, value=float(current_value))

    # Fallback for int without range (not in spec but safe)
    return st.number_input(display_label, value=current_value)


def render_live_config_panel(overlay: ConfigOverlay, service_name: str) -> None:
    """Render the Live Config panel for *service_name* using *overlay*.

    Preconditions:
    - *overlay* is initialised (may be gracefully degraded if Redis is down)
    - Called within a Streamlit execution context

    Postconditions:
    - One widget per parameter in overlay.schema is rendered
    - "Apply" triggers overlay.push() and shows success/error feedback
    - "Reset to default" triggers overlay.reset() and shows info feedback
    - Restart-required parameters show a warning when their value differs from default
    """
    render_live_config_panel_with_auth(overlay, service_name)


def render_live_config_panel_with_auth(
    overlay: ConfigOverlay,
    service_name: str,
    *,
    can_mutate: bool = True,
    denied_message: str = "You are not authorized to mutate live config.",
) -> None:
    """Render live config panel with optional mutation authorization gating."""
    st.subheader("⚙️ Live Config")

    schema = overlay.schema  # {key: ParameterMeta}
    if not schema:
        st.info("No tunable parameters registered for this service.")
        return

    first = True
    for key, meta in schema.items():
        if not first:
            st.divider()
        first = False

        current_value = overlay.get(meta.key)
        env_default = overlay.get_env_default(meta.key)

        # Render the appropriate widget (Req 7.1–7.6)
        new_value = _select_widget(meta, current_value, env_default)

        # Apply / Reset buttons side by side (Req 7.8, 7.10)
        col_apply, col_reset = st.columns(2)

        with col_apply:
            if st.button("Apply", key=f"apply_{service_name}_{key}", disabled=not can_mutate):
                try:
                    if not can_mutate:
                        st.error(denied_message)
                        continue
                    asyncio.run(overlay.push(meta.key, str(new_value)))
                    # Req 7.8
                    st.success("Takes effect within 15 s")
                except ConfigValidationError as exc:
                    # Req 7.9
                    st.error(str(exc))

        with col_reset:
            if st.button("Reset to default", key=f"reset_{service_name}_{key}", disabled=not can_mutate):
                if not can_mutate:
                    st.error(denied_message)
                    continue
                asyncio.run(overlay.reset(meta.key))
                # Req 7.10
                st.info(f"Reset to default: {env_default}")

        # Restart warning (Req 8.3)
        if meta.requires_restart and current_value != env_default:
            st.warning("⚠ This parameter requires a container restart to take effect.")
