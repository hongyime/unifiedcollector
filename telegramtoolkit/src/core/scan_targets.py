#!/usr/bin/env python3
"""
Shared scan-target discovery for processor-backed features.

This centralizes the legacy rules that decide which dialogs should be scanned,
including linked discussion groups for broadcast channels and optional private
message coverage for media downloads.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from telethon.errors import UserNotParticipantError
from telethon.tl.functions.channels import (
    GetParticipantsRequest,
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.types import ChannelParticipantsSearch


def _dialog_name(dialog: Any, entity: Any, fallback_id: str) -> str:
    """Return the best available display name for a dialog target."""
    for value in (
        getattr(dialog, "title", None),
        getattr(dialog, "name", None),
        getattr(entity, "title", None),
        getattr(entity, "first_name", None),
        getattr(entity, "username", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"ID_{fallback_id}"


def _entity_name(entity: Any, fallback_id: str) -> str:
    """Return the best available display name from an entity alone."""
    for value in (
        getattr(entity, "title", None),
        getattr(entity, "first_name", None),
        getattr(entity, "username", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"ID_{fallback_id}"


def _is_group_like(dialog: Any, entity: Any) -> bool:
    """Return True for normal groups and megagroups."""
    if getattr(dialog, "is_group", False):
        return True
    return bool(getattr(dialog, "is_channel", False) and getattr(entity, "megagroup", False))


def _is_private_dialog(dialog: Any, entity: Any) -> bool:
    """Return True for one-to-one dialogs with a non-bot user."""
    if getattr(dialog, "is_user", False):
        return not bool(getattr(entity, "bot", False))
    return False


def _is_broadcast_channel(dialog: Any, entity: Any) -> bool:
    """Return True for non-megagroup channels."""
    return bool(getattr(dialog, "is_channel", False) and not getattr(entity, "megagroup", False))


def _matches_requested_ids(
    requested_ids: Optional[set[str]],
    dialog_entity: Any,
    effective_entity: Any,
    linked_chat_id: Any,
) -> bool:
    """Allow filtering by original dialog id, effective target id, or linked chat id."""
    if requested_ids is None:
        return True

    candidate_ids = {
        str(getattr(dialog_entity, "id", "")),
        str(getattr(effective_entity, "id", "")),
    }
    if linked_chat_id is not None:
        candidate_ids.add(str(linked_chat_id))
    candidate_ids.discard("")
    return any(candidate_id in requested_ids for candidate_id in candidate_ids)


async def _is_linked_chat_member(client: Any, linked_entity: Any) -> bool:
    """Best-effort membership check for a linked discussion group."""
    try:
        await client(
            GetParticipantsRequest(
                linked_entity,
                ChannelParticipantsSearch(""),
                0,
                1,
                hash=0,
            )
        )
        return True
    except UserNotParticipantError:
        return False
    except Exception:
        return False


async def discover_scan_targets(
    client: Any,
    *,
    group_ids: Optional[List[str]] = None,
    include_private_chats: bool = False,
    prefer_linked_discussions: bool = False,
) -> List[Dict[str, Any]]:
    """
    Discover effective scan targets for a processor-backed feature.

    Returns dictionaries with:
    - entity
    - group_id
    - group_name
    - scan_priority
    - cleanup_entities
    """
    requested_ids = {str(group_id) for group_id in group_ids} if group_ids else None
    targets_by_id: Dict[str, Dict[str, Any]] = {}
    discovery_order = 0

    async for dialog in client.iter_dialogs():
        entity = getattr(dialog, "entity", None)
        if entity is None:
            continue

        cleanup_entities: List[Any] = []
        effective_entity = entity
        effective_name = _dialog_name(dialog, entity, str(getattr(entity, "id", "unknown")))
        scan_priority = 99
        linked_chat_id = getattr(entity, "linked_chat_id", None)

        if _is_group_like(dialog, entity):
            scan_priority = 1
        elif include_private_chats and _is_private_dialog(dialog, entity):
            scan_priority = 2
        elif _is_broadcast_channel(dialog, entity):
            if not prefer_linked_discussions:
                scan_priority = 3
            else:
                if not linked_chat_id:
                    continue

                try:
                    linked_entity = await client.get_entity(linked_chat_id)
                except Exception:
                    continue

                if not await _is_linked_chat_member(client, linked_entity):
                    try:
                        await client(JoinChannelRequest(linked_entity))
                        cleanup_entities.append(linked_entity)
                        await asyncio.sleep(1)
                    except Exception:
                        continue

                effective_entity = linked_entity
                effective_name = (
                    f"{_dialog_name(dialog, entity, str(getattr(entity, 'id', 'unknown')))} "
                    f"[Discussion: {_entity_name(linked_entity, str(getattr(linked_entity, 'id', linked_chat_id)))}]"
                )
                scan_priority = 1
        else:
            continue

        if not _matches_requested_ids(requested_ids, entity, effective_entity, linked_chat_id):
            if cleanup_entities:
                await cleanup_scan_target(client, {"cleanup_entities": cleanup_entities})
            continue

        group_id = str(getattr(effective_entity, "id", ""))
        if not group_id:
            if cleanup_entities:
                await cleanup_scan_target(client, {"cleanup_entities": cleanup_entities})
            continue

        target = {
            "entity": effective_entity,
            "group_id": group_id,
            "group_name": effective_name,
            "scan_priority": scan_priority,
            "cleanup_entities": cleanup_entities,
            "discovery_order": discovery_order,
        }
        discovery_order += 1

        existing = targets_by_id.get(group_id)
        if existing is None:
            targets_by_id[group_id] = target
            continue

        existing_cleanup_ids = {
            str(getattr(cleanup_entity, "id", id(cleanup_entity)))
            for cleanup_entity in existing.get("cleanup_entities", [])
        }
        for cleanup_entity in cleanup_entities:
            cleanup_key = str(getattr(cleanup_entity, "id", id(cleanup_entity)))
            if cleanup_key not in existing_cleanup_ids:
                existing.setdefault("cleanup_entities", []).append(cleanup_entity)

    return sorted(
        targets_by_id.values(),
        key=lambda target: (target.get("scan_priority", 99), target.get("discovery_order", 0)),
    )


async def cleanup_scan_target(client: Any, target: Dict[str, Any]) -> None:
    """Leave any temporary linked discussion joins after a scan target completes."""
    for cleanup_entity in target.get("cleanup_entities", []) or []:
        try:
            await asyncio.sleep(1)
            await client(LeaveChannelRequest(cleanup_entity))
        except Exception:
            continue
