from __future__ import annotations

import json
import re
from html import unescape
from typing import Any


NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(?P<data>.*?)</script>', re.DOTALL)
_JSON_UNICODE_RE = re.compile(r'\\u([0-9a-fA-F]{4})')
JSON_ASSIGNMENT_RE = re.compile(r"=\s*(\{.*?\}|\[.*?\]);", re.DOTALL)
MICROFRONTEND_PROPS_RE = re.compile(r"data-react-props=(?P<quote>['\"])(?P<data>.*?)(?P=quote)", re.DOTALL)
ATHLETE_CARD_RE = re.compile(
    r'data-athlete-id="(?P<id>\d+)".*?(?:src|data-src)="(?P<avatar>[^"]*)".*?(?:text-headline|athlete-name)[^>]*>(?P<name>.*?)<',
    re.DOTALL,
)
ATHLETE_LIST_ITEM_RE = re.compile(
    r"<li[^>]*data-athlete-id=['\"](?P<id>\d+)['\"][^>]*>(?P<body>.*?)</li>",
    re.DOTALL,
)
AVATAR_SRC_RE = re.compile(
    r"""(?:
        data-react-props=['"][^'"]*?&quot;src&quot;:&quot;(?P<react_src>[^&]+?)&quot;|
        <img[^>]+src=['"](?P<img_src>[^'"]+)['"]
    )""",
    re.DOTALL | re.VERBOSE,
)
ATHLETE_LINK_RE = re.compile(
    r"<a[^>]+href=['\"]/athletes/\d+['\"][^>]*>(?P<name>.*?)</a>",
    re.DOTALL,
)


def _decode_json_unicode(s: str) -> str:
    """Decode \\uXXXX escapes that html.unescape leaves behind in JS-rendered attributes."""
    return _JSON_UNICODE_RE.sub(lambda m: chr(int(m.group(1), 16)), s)


def ensure_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("entries", "items", "models", "activities", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def first_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None
    return None


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def parse_next_data_json(html: str) -> Any:
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(unescape(match.group("data")))
    except json.JSONDecodeError:
        return None


def parse_json_assignments(html: str) -> list[Any]:
    parsed: list[Any] = []
    for match in JSON_ASSIGNMENT_RE.finditer(html):
        try:
            parsed.append(json.loads(unescape(match.group(1))))
        except json.JSONDecodeError:
            continue
    return parsed


def parse_microfrontend_props(html: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for match in MICROFRONTEND_PROPS_RE.finditer(html):
        raw_value = unescape(match.group("data"))
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            parsed.append(payload)
    return parsed


def extract_profile_feed_entries(html: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    for props in parse_microfrontend_props(html):
        app_context = props.get("appContext")
        if not isinstance(app_context, dict):
            continue
        if app_context.get("page") != "profile" or app_context.get("feedType") != "profile":
            continue
        entries = _extract_profile_entries_from_dict(app_context, assume_profile=True)
        if entries:
            return entries, {**app_context, "source": "microfrontend"}

    next_data_payload = parse_next_data_json(html)
    extracted = _extract_profile_feed_entries_from_payload(next_data_payload, source="next_data")
    if extracted is not None:
        return extracted

    for payload in parse_json_assignments(html):
        extracted = _extract_profile_feed_entries_from_payload(payload, source="inline_json")
        if extracted is not None:
            return extracted
    return [], None


def format_payload_shape(payload: Any) -> str:
    if isinstance(payload, list):
        return f"list[{len(payload)}]"
    if isinstance(payload, dict):
        base = f"keys={_format_key_list(payload.keys())}"
        activity = payload.get("activity")
        if isinstance(activity, dict):
            return f"{base}; activity_keys={_format_key_list(activity.keys())}"
        return base
    if payload is None:
        return "none"
    return type(payload).__name__


def parse_following_cards(html: str) -> list[dict]:
    athletes = []
    for match in ATHLETE_CARD_RE.finditer(html):
        athlete_id = first_int(match.group("id"))
        if athlete_id is None:
            continue
        athletes.append(
            {
                "athlete_id": athlete_id,
                "name": " ".join(unescape(match.group("name")).split()),
                "avatar_url": _decode_json_unicode(unescape(match.group("avatar"))) or None,
                "source": "following_roster",
            }
            )
    if athletes:
        return athletes

    for match in ATHLETE_LIST_ITEM_RE.finditer(html):
        athlete_id = first_int(match.group("id"))
        if athlete_id is None:
            continue
        body = match.group("body")
        name_match = ATHLETE_LINK_RE.search(body)
        if not name_match:
            continue
        avatar_match = AVATAR_SRC_RE.search(body)
        avatar = None
        if avatar_match:
            avatar = avatar_match.group("react_src") or avatar_match.group("img_src")
            avatar = _decode_json_unicode(unescape(avatar)) if avatar else None
        athletes.append(
            {
                "athlete_id": athlete_id,
                "name": " ".join(unescape(name_match.group("name")).split()),
                "avatar_url": avatar,
                "source": "following_roster",
            }
        )
    return athletes


def normalize_activity_photos(
    raw_photos: Any,
    *,
    activity_id: int | None,
    athlete_id: int | None,
    athlete_name: str | None,
    activity_name: str | None,
    start_date_utc: str | None,
    start_date_local: str | None,
    source: str,
) -> list[dict]:
    photos: list[dict] = []
    for raw_photo in ensure_list(raw_photos):
        if not isinstance(raw_photo, dict):
            continue
        photo_id = first_non_empty(raw_photo.get("photo_id"), raw_photo.get("id"))
        resolved_activity_id = first_int(first_non_empty(raw_photo.get("activity_id"), activity_id))
        resolved_athlete_id = first_int(first_non_empty(raw_photo.get("owner_id"), raw_photo.get("athlete_id"), athlete_id))
        if not photo_id or not resolved_activity_id or not resolved_athlete_id:
            continue
        large_url = first_non_empty(raw_photo.get("large"), raw_photo.get("video"))
        thumbnail_url = first_non_empty(raw_photo.get("thumbnail"), raw_photo.get("small"))
        if not large_url and not thumbnail_url:
            continue
        photo_activity = raw_photo.get("activity") if isinstance(raw_photo.get("activity"), dict) else {}
        photos.append(
            {
                "photo_id": str(photo_id),
                "activity_id": resolved_activity_id,
                "athlete_id": resolved_athlete_id,
                "athlete_name": first_non_empty(
                    athlete_name,
                    photo_activity.get("athlete_name"),
                )
                or f"Athlete {resolved_athlete_id}",
                "activity_name": first_non_empty(activity_name, photo_activity.get("name")),
                "caption": first_non_empty(raw_photo.get("caption_escaped"), photo_activity.get("description")),
                "media_type": first_int(raw_photo.get("media_type")) or 1,
                "source_url_large": large_url,
                "source_url_thumbnail": thumbnail_url,
                "start_date_utc": first_non_empty(start_date_utc, photo_activity.get("start_date")),
                "start_date_local": start_date_local,
                "source": source,
            }
        )
    return photos


def _extract_profile_feed_entries_from_payload(
    payload: Any,
    *,
    source: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    if isinstance(payload, dict):
        app_context = payload.get("appContext")
        if isinstance(app_context, dict) and _is_profile_context(app_context):
            entries = _extract_profile_entries_from_dict(app_context, assume_profile=True)
            if entries:
                return entries, {**app_context, "source": source}

        if _is_profile_context(payload):
            entries = _extract_profile_entries_from_dict(payload, assume_profile=True)
            if entries:
                return entries, {**payload, "source": source}

        entries = _extract_profile_entries_from_dict(payload, assume_profile=False)
        if entries:
            return entries, {"source": source, "payload_shape": format_payload_shape(payload)}

        for value in payload.values():
            extracted = _extract_profile_feed_entries_from_payload(value, source=source)
            if extracted is not None:
                return extracted
        return None

    if isinstance(payload, list):
        for item in payload:
            extracted = _extract_profile_feed_entries_from_payload(item, source=source)
            if extracted is not None:
                return extracted
    return None


def _extract_profile_entries_from_dict(payload: dict[str, Any], *, assume_profile: bool) -> list[dict[str, Any]]:
    strong_keys = ("preFetchedEntries", "activities")
    profile_keys = ("entries",)
    heuristic_keys = ("items", "models", "data")

    for key in strong_keys:
        entries = _coerce_activity_entries(payload.get(key), require_activity_hint=False)
        if entries:
            return entries

    for key in profile_keys:
        entries = _coerce_activity_entries(payload.get(key), require_activity_hint=not assume_profile)
        if entries:
            return entries

    for key in heuristic_keys:
        entries = _coerce_activity_entries(payload.get(key), require_activity_hint=True)
        if entries:
            return entries

    return []


def _coerce_activity_entries(value: Any, *, require_activity_hint: bool) -> list[dict[str, Any]]:
    entries = [entry for entry in ensure_list(value) if isinstance(entry, dict)]
    if not entries:
        return []
    if require_activity_hint and not any(_looks_like_activity_entry(entry) for entry in entries):
        return []
    return entries


def _looks_like_activity_entry(payload: dict[str, Any]) -> bool:
    if payload.get("entity") == "Activity":
        return True
    if isinstance(payload.get("activity"), dict):
        return True
    hints = (
        "id",
        "activity_id",
        "activityName",
        "startDate",
        "start_date",
        "start_date_utc",
        "sport_type",
        "type",
        "map",
        "mapAndPhotos",
    )
    return sum(1 for key in hints if key in payload) >= 2


def _is_profile_context(payload: dict[str, Any]) -> bool:
    return payload.get("page") == "profile" or payload.get("feedType") == "profile"


def _format_key_list(keys: Any) -> str:
    values = [str(key) for key in keys]
    preview = ", ".join(values[:6]) if values else "<empty>"
    if len(values) > 6:
        return f"{preview}, ..."
    return preview
