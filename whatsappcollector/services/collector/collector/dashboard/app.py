from __future__ import annotations

import base64
from typing import Any

import httpx
import streamlit as st

from collector.config import settings
from collector.database import database

import sys, os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..', '..', '..'))
from shared.live_config import ConfigOverlay
from shared.dashboard_live_config import render_live_config_panel_with_auth
from shared.live_config import PARAMETER_REGISTRY

_overlay = ConfigOverlay(settings, "collector", settings.REDIS_URL)
_BOOTSTRAP_WIZARD_VERSION = "wizard-v1"
_UNIFIED_OPERATION_MODULES: list[tuple[str, str]] = [
    ("system_health", "System Health"),
    ("service_hub", "Service Dashboard Hub"),
    ("session_controls", "Session Onboarding & Controls"),
    ("findings_hub", "Findings Hub"),
    ("backfill_jobs", "Backfill Jobs"),
    ("dlq", "DLQ"),
    ("pruning_cursors", "Pruning / Service Cursors"),
    ("danger_zone_wipe", "Danger Zone — Database Wipe"),
    ("live_config", "Live Config"),
    ("config_center", "Configuration Center"),
    ("legacy_mapping", "Legacy Functionality Mapping"),
]
_SERVICE_DASHBOARD_LINKS: list[dict[str, str]] = [
    {
        "service": "Dashboard Index",
        "legacy_surface": "dashboard_index",
        "url": "http://localhost:8500",
        "module": "service_hub",
    },
    {
        "service": "Collector",
        "legacy_surface": "collector.dashboard",
        "url": "http://localhost:8501",
        "module": "session_controls/findings_hub/backfill_jobs/dlq/pruning_cursors/danger_zone_wipe/live_config",
    },
    {
        "service": "Media Archival",
        "legacy_surface": "media_archival.dashboard",
        "url": "http://localhost:8502",
        "module": "service_hub",
    },
    {
        "service": "Face Recognition",
        "legacy_surface": "face_recognition.dashboard",
        "url": "http://localhost:8503",
        "module": "service_hub",
    },
    {
        "service": "User Intelligence",
        "legacy_surface": "user_intelligence.dashboard",
        "url": "http://localhost:8504",
        "module": "service_hub",
    },
    {
        "service": "Link Discovery",
        "legacy_surface": "link_discovery.dashboard",
        "url": "http://localhost:8505",
        "module": "service_hub",
    },
    {
        "service": "Bulk Sender",
        "legacy_surface": "bulk_sender.dashboard",
        "url": "http://localhost:8506",
        "module": "service_hub",
    },
]
_LEGACY_FUNCTIONALITY_MATRIX: list[dict[str, str]] = [
    {
        "legacy_surface": "collector.dashboard.session_qr_logout",
        "unified_module": "session_controls",
        "status": "active",
    },
    {
        "legacy_surface": "collector.dashboard.findings_hub",
        "unified_module": "findings_hub",
        "status": "active",
    },
    {
        "legacy_surface": "collector.dashboard.backfill_jobs",
        "unified_module": "backfill_jobs",
        "status": "active",
    },
    {
        "legacy_surface": "collector.dashboard.dlq",
        "unified_module": "dlq",
        "status": "active",
    },
    {
        "legacy_surface": "collector.dashboard.pruning",
        "unified_module": "pruning_cursors",
        "status": "active",
    },
    {
        "legacy_surface": "collector.dashboard.danger_zone_wipe",
        "unified_module": "danger_zone_wipe",
        "status": "active",
    },
    {
        "legacy_surface": "collector.dashboard.live_config",
        "unified_module": "live_config",
        "status": "active",
    },
    {
        "legacy_surface": "collector.dashboard.configuration_center",
        "unified_module": "config_center",
        "status": "active",
    },
    {
        "legacy_surface": "{media_archival,face_recognition,user_intelligence,link_discovery,bulk_sender}.dashboard",
        "unified_module": "service_hub",
        "status": "linked",
    },
]
_ROLE_LEVELS: dict[str, int] = {"viewer": 1, "operator": 2, "admin": 3}


st.set_page_config(page_title="Collector Dashboard", layout="wide")


def _module_label(module_id: str) -> str:
    labels = dict(_UNIFIED_OPERATION_MODULES)
    return labels.get(module_id, module_id)


def _render_operations_navigation_sidebar() -> None:
    module_ids = [module_id for module_id, _ in _UNIFIED_OPERATION_MODULES]
    default_modules = st.session_state.get("_ops_modules_selected")
    if not isinstance(default_modules, list) or not default_modules:
        default_modules = module_ids

    selected_modules = st.multiselect(
        "Unified operations modules",
        options=module_ids,
        default=default_modules,
        format_func=_module_label,
        key="ops_modules_multiselect",
    )
    if not selected_modules:
        st.warning("At least one module is required. Reverting to all modules.")
        selected_modules = module_ids
    st.session_state["_ops_modules_selected"] = selected_modules


def _selected_operations_modules() -> set[str]:
    module_ids = [module_id for module_id, _ in _UNIFIED_OPERATION_MODULES]
    selected = st.session_state.get("_ops_modules_selected")
    if not isinstance(selected, list) or not selected:
        return set(module_ids)
    return set(selected)


def _auth_state() -> dict[str, Any]:
    return {
        "authenticated": bool(st.session_state.get("_auth_authenticated", False)),
        "username": str(st.session_state.get("_auth_username", "")).strip(),
        "role": str(st.session_state.get("_auth_role", "viewer")).strip().lower(),
    }


def _auth_actor_id() -> str | None:
    state = _auth_state()
    if not state["authenticated"]:
        return None
    return state["username"] or None


def _has_role(minimum_role: str) -> bool:
    state = _auth_state()
    if not state["authenticated"]:
        return False
    current = _ROLE_LEVELS.get(state["role"], 0)
    required = _ROLE_LEVELS.get((minimum_role or "viewer").strip().lower(), 99)
    return current >= required


def _deny_mutation_message(minimum_role: str) -> str:
    state = _auth_state()
    if not state["authenticated"]:
        return "Unauthenticated mutation request rejected. Sign in first."
    return (
        f"Insufficient role: '{state['role']}'. "
        f"This action requires '{minimum_role}'."
    )


def _render_auth_sidebar() -> None:
    st.subheader("Authentication")

    if not settings.DASHBOARD_AUTH_REQUIRED:
        st.session_state["_auth_authenticated"] = True
        st.session_state["_auth_username"] = "auth_disabled"
        st.session_state["_auth_role"] = "admin"
        st.info("Dashboard auth disabled by configuration.")
        return

    credentials: list[dict[str, str]] = []
    if settings.DASHBOARD_VIEWER_USERNAME and settings.DASHBOARD_VIEWER_PASSWORD:
        credentials.append(
            {
                "role": "viewer",
                "username": settings.DASHBOARD_VIEWER_USERNAME.strip(),
                "password": settings.DASHBOARD_VIEWER_PASSWORD,
            }
        )
    if settings.DASHBOARD_OPERATOR_USERNAME and settings.DASHBOARD_OPERATOR_PASSWORD:
        credentials.append(
            {
                "role": "operator",
                "username": settings.DASHBOARD_OPERATOR_USERNAME.strip(),
                "password": settings.DASHBOARD_OPERATOR_PASSWORD,
            }
        )
    if settings.DASHBOARD_ADMIN_USERNAME and settings.DASHBOARD_ADMIN_PASSWORD:
        credentials.append(
            {
                "role": "admin",
                "username": settings.DASHBOARD_ADMIN_USERNAME.strip(),
                "password": settings.DASHBOARD_ADMIN_PASSWORD,
            }
        )

    if not credentials:
        st.session_state["_auth_authenticated"] = False
        st.session_state["_auth_username"] = ""
        st.session_state["_auth_role"] = "viewer"
        st.error("Dashboard auth required but no credentials are configured.")
        return

    state = _auth_state()
    if state["authenticated"]:
        st.success(f"Signed in as `{state['username']}` ({state['role']})")
        if st.button("Sign out", key="dashboard_auth_sign_out", use_container_width=True):
            st.session_state["_auth_authenticated"] = False
            st.session_state["_auth_username"] = ""
            st.session_state["_auth_role"] = "viewer"
            st.rerun()
        return

    username = st.text_input("Username", key="dashboard_auth_username")
    password = st.text_input("Password", type="password", key="dashboard_auth_password")
    if st.button("Sign in", key="dashboard_auth_sign_in", use_container_width=True):
        matched = next(
            (
                cred
                for cred in credentials
                if cred["username"] == (username or "").strip() and cred["password"] == password
            ),
            None,
        )
        if matched is None:
            st.error("Invalid credentials.")
            st.session_state["_auth_authenticated"] = False
            st.session_state["_auth_username"] = ""
            st.session_state["_auth_role"] = "viewer"
        else:
            st.session_state["_auth_authenticated"] = True
            st.session_state["_auth_username"] = matched["username"]
            st.session_state["_auth_role"] = matched["role"]
            st.rerun()


def _session_clients() -> list[tuple[str, str]]:
    return list(settings.wa_clients.items())


def _fetch_qr_snapshot(client_url: str) -> dict[str, Any]:
    try:
        response = httpx.get(f"{client_url}/qr", timeout=3.0)
        response.raise_for_status()
        payload = response.json()
        return {
            "status": payload.get("status", "unknown"),
            "session_name": payload.get("session_name"),
            "qr": payload.get("qr"),
            "error": None,
        }
    except Exception as exc:
        return {
            "status": "unreachable",
            "session_name": None,
            "qr": None,
            "error": str(exc),
        }


def _request_logout(session_name: str, client_url: str) -> tuple[bool, str]:
    try:
        response = httpx.post(
            f"{client_url}/logout",
            json={"session_name": session_name},
            timeout=8.0,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}

        if response.status_code == 200:
            return True, str(payload.get("message") or "Logout requested")

        detail = payload.get("error") if isinstance(payload, dict) else response.text
        return False, f"{response.status_code}: {detail}"
    except Exception as exc:
        return False, str(exc)


def _default_bootstrap_values() -> dict[str, Any]:
    """Generate baseline defaults for first-run wizard initialization."""
    return {
        "COLLECTOR_BACKFILL_REQ_PER_MIN": settings.COLLECTOR_BACKFILL_REQ_PER_MIN,
        "COLLECTOR_BACKFILL_POLL_SECONDS": settings.COLLECTOR_BACKFILL_POLL_SECONDS,
        "COLLECTOR_DEDUP_TTL_SECONDS": settings.COLLECTOR_DEDUP_TTL_SECONDS,
        "SESSION_RISK_THRESHOLD": settings.SESSION_RISK_THRESHOLD,
        "SESSION_COOLDOWN_SECONDS": settings.SESSION_COOLDOWN_SECONDS,
        "SESSION_RISK_WINDOW_SECONDS": settings.SESSION_RISK_WINDOW_SECONDS,
        "LANGUAGE_WHITELIST": settings.LANGUAGE_WHITELIST,
    }


def _render_bootstrap_value_editor(key: str, value: Any) -> Any:
    widget_key = f"bootstrap_default_{key}"
    if isinstance(value, bool):
        return st.toggle(key, value=value, key=widget_key)
    if isinstance(value, int) and not isinstance(value, bool):
        return st.number_input(key, value=value, step=1, key=widget_key)
    if isinstance(value, float):
        return st.number_input(key, value=value, key=widget_key)
    return st.text_input(key, value="" if value is None else str(value), key=widget_key)


def _build_bootstrap_config_entries(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "service_name": "collector",
            "config_key": key,
            "value": value,
            "scope": "bootstrap",
            "requires_restart": False,
        }
        for key, value in values.items()
    ]


async def _bootstrap_wizard_panel(bootstrap_state: dict[str, Any]) -> bool:
    """Render first-run setup wizard and return True only when initialized."""
    state = str(bootstrap_state.get("state") or "uninitialized")
    generated_defaults = bootstrap_state.get("generated_defaults") or _default_bootstrap_values()

    st.subheader("🚀 Setup Wizard")
    st.warning("Normal operations are locked until baseline initialization is complete.")

    st.caption("Generated defaults (reviewable before commit)")
    st.json(generated_defaults, expanded=False)

    if state == "initialized":
        st.success("Bootstrap initialization already completed.")
        return True

    if state == "uninitialized":
        st.info("Start the setup wizard to generate and lock the initial baseline configuration.")
        can_start = _has_role("operator")
        if st.button("Start setup wizard", key="bootstrap_start", disabled=not can_start):
            try:
                if not can_start:
                    st.error(_deny_mutation_message("operator"))
                    return False
                await database.start_bootstrap_wizard(
                    wizard_version=_BOOTSTRAP_WIZARD_VERSION,
                    actor_id=_auth_actor_id(),
                    actor_role=_auth_state()["role"],
                    reason="collector dashboard first-run wizard start",
                    generated_defaults=generated_defaults,
                )
                st.success("Setup wizard started. Review defaults and commit to initialize.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to start setup wizard: {exc}")
        return False

    if state == "wizard_in_progress":
        editable_defaults: dict[str, Any] = {}
        st.caption("Review/edit generated defaults before baseline commit")
        for key, value in generated_defaults.items():
            editable_defaults[key] = _render_bootstrap_value_editor(key, value)

        st.caption("Final generated defaults preview")
        st.json(editable_defaults, expanded=False)

        confirmation = st.text_input(
            "Type INITIALIZE to commit baseline",
            key="bootstrap_initialize_confirmation",
        )
        can_commit = _has_role("operator")
        if st.button(
            "Commit bootstrap baseline",
            key="bootstrap_commit",
            type="primary",
            disabled=confirmation.strip() != "INITIALIZE" or not can_commit,
        ):
            try:
                if not can_commit:
                    st.error(_deny_mutation_message("operator"))
                    return False
                await database.commit_bootstrap_baseline(
                    wizard_version=str(bootstrap_state.get("wizard_version") or _BOOTSTRAP_WIZARD_VERSION),
                    generated_defaults=editable_defaults,
                    config_values=_build_bootstrap_config_entries(editable_defaults),
                    actor_id=_auth_actor_id(),
                    actor_role=_auth_state()["role"],
                    reason="collector dashboard first-run baseline commit",
                )
                st.success("Bootstrap baseline committed. Operational controls are now unlocked.")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to commit bootstrap baseline: {exc}")
        return False

    st.error(f"Unknown bootstrap state: {state}")
    return False


def _service_dashboard_hub_panel() -> None:
    st.subheader("Service Dashboard Hub")
    st.caption(
        "Unified shell entrypoint for legacy service dashboards while full feature parity converges."
    )
    st.dataframe(_SERVICE_DASHBOARD_LINKS, use_container_width=True)


async def _configuration_center_panel() -> None:
    st.subheader("Configuration Center")
    st.caption("Unified cross-service control surface (runtime + secret-aware fields).")

    available_services = sorted(PARAMETER_REGISTRY.keys())
    if not available_services:
        st.info("No parameter metadata found.")
        return

    selected_service = st.selectbox(
        "Service",
        options=available_services,
        key="config_center_service_select",
    )

    st.caption("Use 'Live Config' for immediate runtime tunables. Use secret manager below for sensitive keys.")

    secret_records = await database.list_control_secrets(selected_service)
    st.markdown("**Secret Manager**")
    if secret_records:
        st.dataframe(secret_records, use_container_width=True)
    else:
        st.info(f"No stored secrets yet for service `{selected_service}`.")

    st.markdown("**Secret reveal (privileged + audited)**")
    current_role = _auth_state()["role"]
    st.caption(f"Current session role: `{current_role}`")
    reveal_key = st.text_input(
        "Reveal secret key",
        key="config_center_reveal_key",
        placeholder="e.g. MEDIA_BRIDGE_SECRET",
    )
    reveal_confirm = st.text_input(
        "Type REVEAL to confirm privileged reveal",
        key="config_center_reveal_confirmation",
    )
    can_reveal = _has_role("admin")
    if st.button("Reveal secret (audited)", key="config_center_reveal_secret", disabled=not can_reveal):
        normalized_reveal_key = (reveal_key or "").strip()
        if not can_reveal:
            st.error(_deny_mutation_message("admin"))
        elif not normalized_reveal_key:
            st.error("Reveal secret key is required.")
        elif reveal_confirm.strip() != "REVEAL":
            st.error("Type REVEAL to confirm privileged reveal.")
        else:
            try:
                plaintext = await database.get_control_secret_plaintext(
                    selected_service,
                    normalized_reveal_key,
                )
                if plaintext is None:
                    st.warning(
                        f"No secret value found for `{selected_service}.{normalized_reveal_key}`."
                    )
                else:
                    # Emit explicit reveal audit event (masked values remain in audit log).
                    await database.insert_control_change_log_event(
                        event_type="secret_revealed",
                        service_name=selected_service,
                        config_key=normalized_reveal_key,
                        actor_id=_auth_actor_id(),
                        actor_role=_auth_state()["role"],
                        request_id=None,
                        old_value_masked="********",
                        new_value_masked="********",
                        reason="privileged reveal in configuration center",
                        metadata={"action": "reveal", "ui": "config_center"},
                    )
                    st.warning(
                        "Sensitive value revealed for this session only. "
                        "Do not copy into logs or screenshots."
                    )
                    st.code(plaintext)
            except Exception as exc:
                st.error(f"Unable to reveal secret: {exc}")

    secret_key = st.text_input(
        "Secret key",
        key="config_center_secret_key",
        placeholder="e.g. MEDIA_BRIDGE_SECRET",
    )
    secret_value = st.text_input(
        "Secret value",
        key="config_center_secret_value",
        type="password",
    )
    secret_reason = st.text_input(
        "Reason",
        key="config_center_secret_reason",
        placeholder="rotation, onboarding, incident response...",
    )
    can_save_secret = _has_role("operator")
    if st.button("Save secret", key="config_center_save_secret", type="primary", disabled=not can_save_secret):
        normalized_key = (secret_key or "").strip()
        if not can_save_secret:
            st.error(_deny_mutation_message("operator"))
        elif not normalized_key:
            st.error("Secret key is required.")
        elif not secret_value:
            st.error("Secret value is required.")
        else:
            try:
                await database.upsert_control_secret(
                    service_name=selected_service,
                    secret_key=normalized_key,
                    plaintext_value=secret_value,
                    updated_by=_auth_actor_id(),
                    update_reason=(secret_reason or "configuration center secret update").strip(),
                    actor_role=_auth_state()["role"],
                )
                st.success(f"Secret `{normalized_key}` saved for `{selected_service}` (masked-at-rest).")
                st.rerun()
            except Exception as exc:
                st.error(f"Unable to save secret: {exc}")

    st.markdown("**Service parameter metadata**")
    parameter_rows = []
    for meta in PARAMETER_REGISTRY.get(selected_service, []):
        parameter_rows.append(
            {
                "key": meta.key,
                "type": getattr(meta.python_type, "__name__", str(meta.python_type)),
                "default": meta.default,
                "requires_restart": meta.requires_restart,
                "options": ",".join(meta.options) if meta.options else "",
                "description": meta.description,
            }
        )
    if parameter_rows:
        st.dataframe(parameter_rows, use_container_width=True)
    else:
        st.info(f"No parameter metadata registered for `{selected_service}`.")


def _render_legacy_mapping_matrix() -> None:
    st.caption("UI dev notes: legacy-to-unified functionality mapping matrix")
    st.dataframe(_LEGACY_FUNCTIONALITY_MATRIX, use_container_width=True)


def _health_panel() -> None:
    st.subheader("System Health")
    db_ok = False
    try:
        db_ok = st.session_state.get("_db_ok", False)
    except Exception:
        db_ok = False
    st.write(
        {
            "database": "ok" if db_ok else "degraded",
            "wa_clients": len(_session_clients()),
        }
    )


@st.fragment(run_every=5)
def _render_session_controls():
    clients = _session_clients()
    if not clients:
        st.warning("No wa-client sessions configured.")
        return

    st.caption("🔄 Auto-refreshing session status every 5s")
    for session_name, client_url in clients:
        snapshot = _fetch_qr_snapshot(client_url)
        status = str(snapshot.get("status") or "unknown")
        resolved_session = str(snapshot.get("session_name") or session_name)

        with st.container(border=True):
            col1, col2 = st.columns([3, 2])
            with col1:
                st.write({"session_name": resolved_session, "status": status, "url": client_url})
            with col2:
                can_logout = _has_role("operator")
                if st.button(f"Logout {resolved_session}", key=f"logout_{resolved_session}", disabled=not can_logout):
                    if not can_logout:
                        st.error(_deny_mutation_message("operator"))
                        continue
                    ok, message = _request_logout(resolved_session, client_url)
                    if ok:
                        st.success(f"{resolved_session}: {message}")
                    else:
                        st.error(f"{resolved_session}: {message}")

            qr = snapshot.get("qr")
            if status == "waiting" and qr:
                try:
                    st.image(base64.b64decode(str(qr)), caption=f"Scan to pair {resolved_session}")
                except Exception as exc:
                    st.warning(f"Unable to decode QR for {resolved_session}: {exc}")
            elif status == "connected":
                st.success(f"{resolved_session} connected")
            elif status == "unreachable":
                st.warning(f"Unable to contact {resolved_session}: {snapshot.get('error')}")


def _qr_and_logout_panel() -> None:
    st.subheader("Session Onboarding & Controls")
    
    # Render the auto-refreshing fragment for session statuses
    _render_session_controls()

    st.divider()
    clients = _session_clients()
    st.caption("Bulk logout requires explicit operator confirmation.")
    logout_all_confirm = st.text_input(
        "Type LOGOUT ALL to enable",
        key="logout_all_confirmation",
    )
    can_bulk_logout = _has_role("operator")
    if st.button(
        "Logout ALL sessions",
        key="logout_all_button",
        disabled=logout_all_confirm.strip() != "LOGOUT ALL" or not can_bulk_logout,
    ):
        if not can_bulk_logout:
            st.error(_deny_mutation_message("operator"))
            return
        success_count = 0
        errors: list[str] = []
        for session_name, client_url in clients:
            ok, message = _request_logout(session_name, client_url)
            if ok:
                success_count += 1
            else:
                errors.append(f"{session_name}: {message}")
        if success_count:
            st.success(f"Logout requested for {success_count} session(s).")
        if errors:
            st.error("; ".join(errors))


async def _findings_hub_panel() -> None:
    st.subheader("Findings Hub")
    try:
        group_rows = await database.get_group_chats()
        hub_jid = await database.get_system_config("findings_hub_jid")

        if hub_jid:
            st.success(f"Active hub group JID: `{hub_jid}`")
        else:
            st.warning("Findings Hub not configured. No hub group JID persisted yet.")

        st.caption(
            "Copy the JID of your findings group and set "
            "`FINDINGS_HUB_GROUP_JID=<jid>` in `.env`, then restart wa-client-ts containers."
        )

        if group_rows:
            st.dataframe(
                [{"name": row["name"], "jid": row["jid"]} for row in group_rows],
                use_container_width=True,
            )
        else:
            st.info("No group chats detected yet. Connect a WhatsApp session to populate this list.")
    except Exception as exc:
        st.error(f"Findings Hub panel error: {exc}")


async def _backfill_jobs_panel() -> None:
    st.subheader("Backfill Jobs")
    rows = await database.get_backfill_jobs(limit=200)
    st.dataframe([dict(row) for row in rows], use_container_width=True)


def _dlq_panel() -> None:
    st.subheader("DLQ")
    st.caption("Depth and actions are managed by worker metrics/logging.")
    st.button("Retry DLQ", disabled=True)
    st.button("Clear DLQ", disabled=True)


async def _pruning_panel() -> None:
    st.subheader("Pruning / Service Cursors")
    cursors = await database.get_service_cursors()
    st.dataframe([dict(row) for row in cursors], use_container_width=True)
    confirmation = st.text_input("Type PRUNE to enable")
    can_prune = _has_role("operator")
    st.button("Run prune now", disabled=confirmation != "PRUNE" or not can_prune)


async def _wipe_panel() -> None:
    st.subheader("Danger Zone — Database Wipe")
    st.caption("Destructive action: this truncates table data and cannot be undone.")

    schemas = settings.wipeable_schemas
    table_counts = await database.get_schema_table_counts(schemas)
    if table_counts:
        st.dataframe([dict(row) for row in table_counts], use_container_width=True)

    target = st.selectbox(
        "Target",
        options=["ALL"] + schemas,
        key="wipe_target",
    )
    expected = "WIPE ALL" if target == "ALL" else f"WIPE {target}"
    typed = st.text_input(
        f"Type {expected} to confirm",
        key="wipe_confirmation",
    )

    can_wipe = _has_role("admin")
    if st.button(
        "Execute data wipe",
        key="wipe_execute",
        type="primary",
        disabled=typed.strip() != expected or not can_wipe,
    ):
        if not can_wipe:
            st.error(_deny_mutation_message("admin"))
            return
        targets = schemas if target == "ALL" else [target]
        try:
            results = await database.wipe_schemas(targets)
            st.success("Data wipe completed.")
            st.dataframe(results, use_container_width=True)
        except Exception as exc:
            st.error(f"Data wipe failed: {exc}")


async def _render_async() -> None:
    # asyncio.run() creates a new event loop on every Streamlit render.
    # If a previous render was interrupted before close(), the pool from the
    # dead loop stays set and causes InterfaceError on reuse. Force-reset it
    # so connect() always creates a fresh pool in the current loop.
    database.pool = None
    await database.connect()
    try:
        st.session_state["_db_ok"] = await database.health_check()

        selected_modules = _selected_operations_modules()

        bootstrap_state = await database.get_control_bootstrap_state()
        if str(bootstrap_state.get("state") or "uninitialized") != "initialized":
            if "system_health" in selected_modules:
                _health_panel()
            await _bootstrap_wizard_panel(bootstrap_state)
            if "legacy_mapping" in selected_modules:
                with st.expander("🧭 Legacy functionality mapping (UI dev notes)", expanded=False):
                    _render_legacy_mapping_matrix()
            return

        if "system_health" in selected_modules:
            _health_panel()
            st.divider()

        if "service_hub" in selected_modules:
            _service_dashboard_hub_panel()
            st.divider()

        if "session_controls" in selected_modules:
            _qr_and_logout_panel()
            st.divider()

        if "findings_hub" in selected_modules:
            await _findings_hub_panel()
            st.divider()

        if "backfill_jobs" in selected_modules or "dlq" in selected_modules:
            col1, col2 = st.columns(2)
            with col1:
                if "backfill_jobs" in selected_modules:
                    await _backfill_jobs_panel()
            with col2:
                if "dlq" in selected_modules:
                    _dlq_panel()
            st.divider()

        if "pruning_cursors" in selected_modules or "danger_zone_wipe" in selected_modules:
            col1, col2 = st.columns(2)
            with col1:
                if "pruning_cursors" in selected_modules:
                    await _pruning_panel()
            with col2:
                if "danger_zone_wipe" in selected_modules:
                    await _wipe_panel()
            st.divider()

        if "live_config" in selected_modules:
            with st.expander("⚙️ Live Config", expanded=False):
                render_live_config_panel_with_auth(
                    _overlay,
                    "collector",
                    can_mutate=_has_role("operator"),
                    denied_message=_deny_mutation_message("operator"),
                )

        if "config_center" in selected_modules:
            st.divider()
            await _configuration_center_panel()

        if "legacy_mapping" in selected_modules:
            with st.expander("🧭 Legacy functionality mapping (UI dev notes)", expanded=False):
                _render_legacy_mapping_matrix()
    finally:
        await database.close()


def main() -> None:
    st.title("WhatsApp Collector")
    st.caption("Unified operations dashboard shell")
    with st.sidebar:
        st.header("Controls")
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.caption("Dashboard auto-refreshes session status every 5s via fragments.")
        st.divider()
        _render_auth_sidebar()
        st.divider()
        st.subheader("Navigation")
        _render_operations_navigation_sidebar()
    import asyncio

    asyncio.run(_render_async())


if __name__ == "__main__":
    main()
