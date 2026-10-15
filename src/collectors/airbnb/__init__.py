"""Airbnb profile probe (Do Next #3).

URL: https://www.airbnb.com/users/show/<numeric_id> OR
     https://www.airbnb.com/users/profile/<username>

Note: Airbnb has been extremely SPA-heavy for years - the initial
HTML response contains most profile fields inside a Redux-style JSON
blob (`data-injector`), which we can regex. If Airbnb ships a
Cloudflare challenge / CAPTCHA, we get an HTML wall with no fields
and return exists=False without erroring.

Source: tools.myosint.training "Airbnb User Bookmarklet".
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult

logger = logging.getLogger(__name__)

# Airbnb serializes user data inside a data-state JSON blob. Field names
# below match what the bookmarklet at tools.myosint.training reads.
_DISPLAY_RE = re.compile(r'"first_name"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_LAST_RE = re.compile(r'"last_name"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_AVATAR_RE = re.compile(r'"picture_url"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_PROFILE_BIO_RE = re.compile(
    r'"(?:hostProfileDescription|aboutMe|about_me|about|biography|bio|htmlText)"\s*:\s*"((?:[^"\\]|\\.)*?)"',
    re.IGNORECASE,
)
_HOST_SINCE_RE = re.compile(r'"created_at"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_SUPERHOST_RE = re.compile(r'"is_superhost"\s*:\s*(true|false)')
_LOCATION_RE = re.compile(r'"location"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_LISTINGS_RE = re.compile(r'"listings_count"\s*:\s*(\d+)')
_REVIEWS_RE = re.compile(r'"reviews_count"\s*:\s*(\d+)')

# Public persisted-query identifiers used by Airbnb's web client. The API key
# is also public web-client configuration; deployments may override it.
_PDP_SECTIONS_HASH = "80c7889b4b0027d99ffea830f6c0d4911a6e863a957cbe1044823f0fc746bf1f"
_PDP_REVIEWS_HASH = "dec1c8061483e78373602047450322fd474e79ba9afa8d3dbbc27f504030f91d"
_PDP_AVAILABILITY_HASH = "8f08e03c7bd16fcad3c92a3592c19a8b559a0d0855a84028d1163d4733ed9ade"
_USER_PROFILE_HASH = "a56d8909f271740ccfef23dd6c34d098f194f4a6e7157f244814c5610b8ad76a"
_AIRBNB_API_KEY_RE = re.compile(r'"api_config"\s*:\s*\{[^{}]*"key"\s*:\s*"([^"]+)"')
_AIRBNB_HOSTS = frozenset({
    "airbnb.com", "www.airbnb.com", "airbnb.com.sg", "www.airbnb.com.sg",
    "airbnb.co.uk", "www.airbnb.co.uk", "airbnb.co.kr", "www.airbnb.co.kr",
})
_LISTING_PATH_RE = re.compile(r"^/rooms/(\d{1,24})/?$")
_HOST_PATH_RE = re.compile(r"^/users/show/(\d{1,20})/?$")
_HOST_PROFILE_PATH_RE = re.compile(r"^/users/profile/([A-Za-z0-9._-]{1,100})/?$")
_AIRBNB_DEFAULT_BASE_URL = "https://www.airbnb.com"
_AIRBNB_API_KEY_CACHE: tuple[str, str, float] | None = None
_AIRBNB_REGION_CACHE: tuple[str, float] | None = None


class _AirbnbPublicHttpError(RuntimeError):
    def __init__(self, status_code: int, retry_after_seconds: int | None = None):
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"public_http_{self.status_code}")


_AIRBNB_SAFE_ERROR_CODES = frozenset({
    "graphql_payload_unavailable",
    "graphql_query_failed",
    "host_profile_page_unavailable",
    "host_profile_payload_unavailable",
    "host_profile_unavailable",
    "host_listing_unavailable",
    "listing_page_payload_unavailable",
    "listing_page_unavailable",
    "listing_payload_unavailable",
    "public_api_key_unavailable",
    "public_handoff_rejected",
    "public_page_unavailable",
    "public_redirect_limit",
    "public_redirect_rejected",
    "unsupported_public_page",
})


def _airbnb_error_code(exc: Exception) -> str:
    if isinstance(exc, _AirbnbPublicHttpError):
        return f"public_http_{exc.status_code}"
    message = str(exc)
    return message if message in _AIRBNB_SAFE_ERROR_CODES else type(exc).__name__.lower()


def _retry_after_seconds(value: Any, *, now: datetime | None = None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        seconds = int(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = int((retry_at - (now or datetime.now(timezone.utc))).total_seconds())
    return min(7 * 24 * 60 * 60, max(1, seconds))


def _decode_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


def _walk_dicts(value: Any, *, limit: int = 20_000):
    stack = [value]
    count = 0
    while stack and count < limit:
        current = stack.pop()
        if isinstance(current, dict):
            count += 1
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current[:2000]))


def _find_scalar(value: Any, keys: tuple[str, ...]) -> Any:
    key_set = {key.lower() for key in keys}
    for row in _walk_dicts(value):
        for key, candidate in row.items():
            if key.lower() in key_set and isinstance(candidate, (str, int, float)) and str(candidate).strip():
                return candidate
    return None


def _find_node(value: Any, keys: tuple[str, ...]) -> Any:
    key_set = {key.lower() for key in keys}
    for row in _walk_dicts(value):
        for key, candidate in row.items():
            if key.lower() in key_set:
                return candidate
    return None


def _text_value(value: Any, limit: int = 500) -> str | None:
    if isinstance(value, dict):
        for key in ("localizedReview", "translation", "text", "comment", "value", "name"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)) and str(candidate).strip():
                return str(candidate).strip()[:limit]
        return None
    if isinstance(value, (str, int, float)) and str(value).strip():
        return str(value).strip()[:limit]
    return None


def _number_value(value: Any, *, integer: bool = False) -> int | float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number > 1_000_000:
        return None
    return int(number) if integer else number


def _percentage_value(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().removesuffix("%")
    number = _number_value(value)
    return float(number) if number is not None and number <= 100 else None


def _response_rate_value(value: Any) -> float | None:
    percentage = _percentage_value(value)
    if percentage is not None or not isinstance(value, str):
        return percentage
    match = re.search(r"\b(\d{1,3}(?:[.,]\d+)?)\s*%", value)
    if not match:
        return None
    return _percentage_value(match.group(1).replace(",", "."))


def _https_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return value[:2000]


def _graphql_sections(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    presentation = data.get("presentation")
    if not isinstance(presentation, dict):
        return {}
    detail = presentation.get("stayProductDetailPage")
    if not isinstance(detail, dict):
        return {}
    section_container = detail.get("sections")
    sections = (
        section_container.get("sections")
        if isinstance(section_container, dict)
        else section_container
    )
    result: dict[str, dict[str, Any]] = {}
    for row in sections if isinstance(sections, list) else []:
        if not isinstance(row, dict):
            continue
        section_id = row.get("sectionId")
        if isinstance(section_id, str):
            result[section_id] = row
    return result


def _airbnb_text(value: Any, limit: int = 12_000) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return text[:limit] or None


def _airbnb_photos(sections: Any) -> list[dict[str, str]]:
    photos: dict[str, dict[str, str]] = {}
    for row in _walk_dicts(sections):
        if row.get("__typename") != "PhotoTourModalSection":
            continue
        media_items = row.get("mediaItems")
        if not isinstance(media_items, list):
            continue
        for item in media_items[:60]:
            if not isinstance(item, dict):
                continue
            image_url = _https_value(item.get("baseUrl") or item.get("picture") or item.get("url"))
            if not image_url:
                continue
            photos[image_url] = {
                "url": image_url,
                "title": _text_value(item.get("accessibilityLabel"), 500) or "",
            }
    return list(photos.values())[:60]


def _airbnb_amenities(payload: dict[str, Any], sections: Any) -> list[dict[str, Any]]:
    data = payload.get("data")
    node = data.get("node") if isinstance(data, dict) else None
    presentation = node.get("pdpPresentation") if isinstance(node, dict) else None
    amenity_root = presentation.get("amenities") if isinstance(presentation, dict) else None
    groups = amenity_root.get("seeAllAmenitiesGroups") if isinstance(amenity_root, dict) else None
    if not isinstance(groups, list):
        groups = []
        for row in _walk_dicts(sections):
            if row.get("__typename") == "AmenitiesSection":
                candidate = row.get("seeAllAmenitiesGroups") or row.get("amenitiesGroups")
                if isinstance(candidate, list):
                    groups = candidate
                    break

    result = []
    for group in groups[:40]:
        if not isinstance(group, dict):
            continue
        values = []
        raw_amenities = group.get("amenities")
        for amenity in raw_amenities[:100] if isinstance(raw_amenities, list) else []:
            if not isinstance(amenity, dict):
                continue
            title = _text_value(amenity.get("title") or amenity.get("name"), 300)
            if not title:
                continue
            available = amenity.get("available")
            values.append({
                "title": title,
                "subtitle": _text_value(amenity.get("subtitle"), 500),
                "available": available if isinstance(available, bool) else None,
            })
        if values:
            result.append({
                "title": _text_value(group.get("title"), 300),
                "amenities": values,
            })
    return result[:40]


def _airbnb_calendar_date(value: Any, month: Any = None) -> str | None:
    text = _text_value(value, 64)
    if text:
        candidate = text[:10]
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass
    if isinstance(value, int) and 1 <= value <= 31 and isinstance(month, dict):
        month_value = month.get("month") or month.get("monthNumber")
        year_value = month.get("year")
        if isinstance(month_value, str):
            match = re.search(r"(\d{4})-(\d{1,2})", month_value)
            if match:
                year_value, month_value = int(match.group(1)), int(match.group(2))
        try:
            return date(int(year_value), int(month_value), value).isoformat()
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _parse_airbnb_calendar(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    merlin = data.get("merlin") if isinstance(data, dict) else None
    calendar = merlin.get("pdpAvailabilityCalendar") if isinstance(merlin, dict) else None
    months = calendar.get("calendarMonths") if isinstance(calendar, dict) else None
    if not isinstance(months, list):
        return []
    result: dict[str, dict[str, Any]] = {}
    for month in months[:12]:
        if not isinstance(month, dict):
            continue
        days = month.get("days") or month.get("calendarDays")
        if not isinstance(days, list):
            continue
        for day in days:
            if not isinstance(day, dict):
                continue
            day_date = _airbnb_calendar_date(
                day.get("calendarDate") or day.get("date") or day.get("day"), month,
            )
            if not day_date:
                continue
            available = day.get("available")
            if not isinstance(available, bool):
                available = day.get("isAvailable")
            if not isinstance(available, bool):
                status = _text_value(day.get("availability") or day.get("status"), 50)
                available = status.lower() in {"available", "bookable"} if status else None
            bookable = day.get("bookable")
            if not isinstance(bookable, bool):
                bookable = day.get("canCheckin")
            price_value = _find_scalar(day, ("localizedPrice", "priceString", "price", "amount"))
            price_currency = _text_value(_find_scalar(day, ("currencyCode", "currency")), 8)
            minimum_nights = _number_value(
                _find_scalar(day, ("minNights", "minimumNights", "minimumStay")), integer=True,
            )
            result[day_date] = {
                "date": day_date,
                "available": available,
                "bookable": bookable if isinstance(bookable, bool) else None,
                "minimum_nights": minimum_nights,
                "price": _text_value(price_value, 100) if price_value is not None else None,
                "currency": price_currency,
            }
    return [result[key] for key in sorted(result)][:370]


def _first_available_stay(availability: list[dict[str, Any]], today: date) -> tuple[date, date] | None:
    by_date = {
        str(row.get("date")): row
        for row in availability
        if isinstance(row, dict) and row.get("date")
    }

    def is_available(row: dict[str, Any] | None) -> bool:
        return bool(row) and (
            row.get("available") is True
            or (row.get("available") is None and row.get("bookable") is True)
        )

    for row in availability:
        if not isinstance(row, dict) or not is_available(row) or not row.get("date"):
            continue
        try:
            check_in = date.fromisoformat(str(row["date"]))
        except ValueError:
            continue
        if check_in < today:
            continue
        minimum_nights = row.get("minimum_nights")
        try:
            nights = max(1, min(30, int(minimum_nights)))
        except (TypeError, ValueError, OverflowError):
            nights = 1
        check_out = check_in + timedelta(days=nights)
        if all(is_available(by_date.get((check_in + timedelta(days=offset)).isoformat()))
               for offset in range(nights)):
            return check_in, check_out
    return None


def _airbnb_money(value: Any, currency_hint: str | None = None) -> tuple[float | None, str | None]:
    explicit_currency: str | None = None
    if isinstance(value, dict):
        explicit_currency = _text_value(
            value.get("currencyCode") or value.get("currency"), 8,
        )
        value = value.get("amount") or value.get("value") or value.get("localized") or value.get("formatted")
    if isinstance(value, (int, float)):
        amount = _number_value(value)
        return amount, explicit_currency or currency_hint
    text = _text_value(value, 100)
    if not text:
        return None, explicit_currency or currency_hint
    amount_match = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not amount_match:
        return None, explicit_currency or currency_hint
    try:
        amount = float(amount_match.group(0).replace(",", ""))
    except ValueError:
        return None, explicit_currency or currency_hint
    code_match = re.search(r"\b([A-Z]{3})\b", text)
    if code_match:
        explicit_currency = code_match.group(1)
    elif "S$" in text:
        explicit_currency = "SGD"
    elif "A$" in text:
        explicit_currency = "AUD"
    elif "£" in text:
        explicit_currency = "GBP"
    elif "€" in text:
        explicit_currency = "EUR"
    elif "¥" in text:
        explicit_currency = "JPY"
    return _number_value(amount), explicit_currency or currency_hint


def _parse_airbnb_price(payload: dict[str, Any], currency_hint: str = "USD") -> tuple[float | None, str | None]:
    sections = _graphql_sections(payload)
    for section in sections.values():
        for row in _walk_dicts(section):
            display = row.get("structuredDisplayPrice")
            if not isinstance(display, dict):
                continue
            primary = display.get("primaryLine") if isinstance(display.get("primaryLine"), dict) else {}
            for key in ("discountedPrice", "originalPrice", "price"):
                if primary.get(key) not in (None, ""):
                    return _airbnb_money(primary[key], currency_hint)
    return None, currency_hint


def _normalize_host_id(value: Any) -> str | None:
    raw = _text_value(value, 100)
    if not raw:
        return None
    if raw.isdigit():
        return raw
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    except Exception:
        return None
    match = re.search(r"(?:User|UserProfile):([0-9]{1,20})$", decoded)
    return match.group(1) if match else None


def _parse_airbnb_listing(payload: dict[str, Any], listing_id: str) -> dict[str, Any] | None:
    sections = _graphql_sections(payload)
    if not sections:
        return None

    title_section = sections.get("TITLE_DEFAULT", {})
    location_section = sections.get("LOCATION_DEFAULT", {})
    reviews_section = sections.get("REVIEWS_DEFAULT", {})
    host_section = sections.get("MEET_YOUR_HOST", {})
    data = payload.get("data")
    node = data.get("node") if isinstance(data, dict) else None
    pdp_presentation = node.get("pdpPresentation") if isinstance(node, dict) else None
    host_info = pdp_presentation.get("hostInfo") if isinstance(pdp_presentation, dict) else None
    host_info = host_info if isinstance(host_info, dict) else {}
    passport_data = host_info.get("passportData")
    passport_data = passport_data if isinstance(passport_data, dict) else {}
    title = _text_value(_find_scalar(title_section, ("title", "listingTitle", "headline", "name")))
    location = _text_value(_find_scalar(location_section, ("subtitle", "location", "city", "address")))
    rating = _number_value(_find_scalar(reviews_section, ("overallRating", "averageRating", "rating")))
    reviews_count = _number_value(
        _find_scalar(reviews_section, ("reviewsCount", "reviewCount", "reviews_count")), integer=True,
    )

    host_card = _find_node(host_section, ("cardData",))
    if not isinstance(host_card, dict):
        host_card = host_section
    host_id = _normalize_host_id(_find_scalar(host_card, ("userId", "hostId", "hostID", "id")))
    host_id = host_id or _normalize_host_id(passport_data.get("userId"))
    host_name = _text_value(_find_scalar(host_card, ("displayName", "hostName", "name")), 300)
    host_name = host_name or _text_value(passport_data.get("name"), 300)
    host_avatar = _https_value(_find_scalar(host_card, ("avatarUrl", "profilePicture", "pictureUrl", "avatar")))
    host_avatar = host_avatar or _https_value(passport_data.get("profilePictureUrl"))
    host_rating = _number_value(_find_scalar(host_card, ("ratingAverage", "averageRating", "rating")))
    host_rating = host_rating if host_rating is not None else _number_value(passport_data.get("ratingAverage"))
    host_reviews = _number_value(
        _find_scalar(host_card, ("reviewsCount", "reviewCount")), integer=True,
    )
    host_reviews = host_reviews if host_reviews is not None else _number_value(
        passport_data.get("ratingCount"), integer=True,
    )
    host_since = _text_value(_find_scalar(host_card, ("timeAsHost", "hostSince", "createdAt")), 64)
    host_bio = _airbnb_text(
        _find_scalar(host_section, ("htmlText", "hostProfileDescription", "description", "bio")),
    )
    if host_bio is None:
        host_profile_section = next((
            row for row in _walk_dicts(sections)
            if row.get("__typename") == "HostProfileSection"
        ), {})
        host_bio = _airbnb_text(
            _find_scalar(host_profile_section, ("htmlText", "description", "bio")),
        )
    if host_bio is None:
        host_bio = _airbnb_text(
            _find_scalar(host_info, ("htmlText", "hostProfileDescription", "description", "bio")),
        )
    host_response_rate = _response_rate_value(
        _find_scalar(host_card, ("responseRate", "responseRatePercentage", "response_rate")),
    )
    if host_response_rate is None:
        host_response_rate = _response_rate_value(host_info.get("responseRateText"))

    listing_sections = {key: value for key, value in sections.items() if key != "MEET_YOUR_HOST"}
    image_url = _https_value(_find_scalar(listing_sections, ("imageUrl", "pictureUrl", "baseUrl", "image")))
    description_section = sections.get("DESCRIPTION_DEFAULT", {})
    description = _airbnb_text(
        _find_scalar(description_section, ("htmlText", "description", "text", "summary")),
    )
    if description is None:
        description_node = next((
            row for row in _walk_dicts(sections)
            if row.get("__typename") == "PdpDescriptionSection"
        ), {})
        description = _airbnb_text(
            _find_scalar(description_node, ("htmlText", "description", "text", "summary")),
        )
    if description is None:
        description_modal = sections.get("DESCRIPTION_MODAL", {}).get("section", {})
        items = description_modal.get("items") if isinstance(description_modal, dict) else None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            html = item.get("html")
            candidate = _airbnb_text(html.get("htmlText")) if isinstance(html, dict) else None
            if candidate:
                description = candidate
                break
    photos = _airbnb_photos(sections)
    if image_url and not photos:
        photos = [{"url": image_url, "title": ""}]
    amenities = _airbnb_amenities(payload, sections)
    property_type = _text_value(_find_scalar(sections, ("propertyType", "roomType")), 200)
    bedrooms = _number_value(_find_scalar(sections, ("bedrooms", "bedroomCount")))
    bathrooms = _number_value(_find_scalar(sections, ("bathrooms", "bathroomCount")))
    max_guests = _number_value(_find_scalar(sections, ("maxGuestCapacity", "maxGuests", "guestCapacity")), integer=True)

    return {
        "listing_id": listing_id,
        "host_id": host_id,
        "host_name": host_name,
        "host_profile_url": f"https://www.airbnb.com/users/show/{host_id}" if host_id else None,
        "host_avatar_url": host_avatar,
        "host_rating": host_rating,
        "host_reviews_count": host_reviews,
        "host_since": host_since,
        "host_bio": host_bio,
        "host_response_rate": host_response_rate,
        "title": title,
        "url": f"https://www.airbnb.com/rooms/{listing_id}",
        "location": location,
        "image_url": image_url,
        "description": description,
        "photos": photos,
        "amenities": amenities,
        "rating": rating,
        "reviews_count": reviews_count,
        "property_type": property_type,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "max_guests": max_guests,
    }


def _parse_airbnb_reviews(payload: dict[str, Any], listing_id: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    presentation = data.get("presentation") if isinstance(data, dict) else None
    detail = presentation.get("stayProductDetailPage") if isinstance(presentation, dict) else None
    review_root = detail.get("reviews") if isinstance(detail, dict) else None
    raw_reviews = review_root.get("reviews", []) if isinstance(review_root, dict) else []
    results = []
    for row in raw_reviews[:10] if isinstance(raw_reviews, list) else []:
        if not isinstance(row, dict):
            continue
        review_id = _text_value(row.get("id"), 200)
        if not review_id:
            continue
        reviewer = row.get("reviewer") if isinstance(row.get("reviewer"), dict) else {}
        reviewer_id = _text_value(reviewer.get("id") or reviewer.get("userId"), 100)
        comment = _text_value(row.get("commentV2"), 12_000) or _text_value(row.get("comments"), 12_000)
        language = _text_value(row.get("language"), 32)
        results.append({
            "review_id": review_id,
            "listing_id": listing_id,
            "reviewer_id": reviewer_id,
            "review_date": _text_value(row.get("createdAt"), 64),
            "rating": _number_value(row.get("rating")),
            "comments": comment,
            "language": language,
        })
    return results


def _parse_airbnb_host_profile(payload: dict[str, Any], host_id: str) -> dict[str, Any]:
    """Extract the public host fields returned by Airbnb's GetUserProfile query."""
    profile = _find_node(payload, ("userProfile", "profile"))
    if not isinstance(profile, dict):
        profile = payload

    first_name = _text_value(_find_scalar(profile, ("firstName",)), 150)
    last_name = _text_value(_find_scalar(profile, ("lastName",)), 150)
    display_name = _text_value(_find_scalar(profile, ("displayName", "name")), 300)
    if not display_name:
        display_name = " ".join(part for part in (first_name, last_name) if part) or None

    bio = _airbnb_text(_find_scalar(
        profile,
        ("hostProfileDescription", "aboutMe", "about", "biography", "bio", "htmlText", "description"),
    ))
    host_since = _text_value(_find_scalar(profile, ("timeAsHost", "hostSince", "memberSince", "createdAt")), 64)
    return {
        "host_id": host_id,
        "display_name": display_name,
        "profile_url": f"https://www.airbnb.com/users/show/{host_id}",
        "avatar_url": _https_value(_find_scalar(
            profile,
            ("profilePictureUrl", "pictureUrl", "avatarUrl", "profilePicture", "avatar"),
        )),
        "rating": _number_value(_find_scalar(profile, ("ratingAverage", "averageRating", "rating"))),
        "reviews_count": _number_value(
            _find_scalar(profile, ("ratingCount", "reviewsCount", "reviewCount")), integer=True,
        ),
        "host_since": host_since,
        "bio": bio,
        "response_rate": _response_rate_value(
            _find_scalar(profile, ("responseRate", "responseRatePercentage", "response_rate")),
        ),
    }


def _parse_airbnb_host_profile_page(html: str, host_id: str) -> dict[str, Any] | None:
    """Read host fields from the public page's deferred GraphQL JSON island."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        state_node = soup.select_one("#data-deferred-state-0")
        if state_node is None:
            return None
        state = json.loads(state_node.get_text())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    candidates: list[dict[str, Any]] = []
    if isinstance(state, dict):
        rows = state.get("niobeClientData")
        if isinstance(rows, list):
            candidates.extend(
                row[1] for row in rows
                if isinstance(row, list) and len(row) > 1 and isinstance(row[1], dict)
            )
        candidates.append(state)
    for payload in candidates:
        profile = _parse_airbnb_host_profile(payload, host_id)
        if any(profile.get(key) for key in (
            "display_name", "avatar_url", "rating", "reviews_count",
            "host_since", "bio", "response_rate",
        )):
            return profile
    return None


def _stays_detail_variables(
    listing_id: str,
    check_in: date | None = None,
    check_out: date | None = None,
) -> dict[str, Any]:
    def encode(prefix: str) -> str:
        return base64.b64encode(f"{prefix}:{listing_id}".encode()).decode()

    return {
        "id": encode("StayListing"),
        "demandStayListingId": encode("DemandStayListing"),
        "pdpSectionsRequest": {
            "adults": "1",
            "bypassTargetings": False,
            "categoryTag": None,
            "causeId": None,
            "children": None,
            "disasterId": None,
            "discountedGuestFeeVersion": None,
            "displayExtensions": None,
            "federatedSearchId": None,
            "forceBoostPriorityMessageType": None,
            "infants": None,
            "interactionType": None,
            "layouts": ["SIDEBAR", "SINGLE_COLUMN"],
            "pets": 0,
            "pdpTypeOverride": None,
            "photoId": None,
            "preview": False,
            "previousStateCheckIn": None,
            "previousStateCheckOut": None,
            "priceDropSource": None,
            "privateBooking": False,
            "promotionUuid": None,
            "relaxedAmenityIds": None,
            "searchId": None,
            "selectedCancellationPolicyId": None,
            "selectedRatePlanId": None,
            "splitStays": None,
            "staysBookingMigrationEnabled": False,
            "translateUgc": None,
            "useNewSectionWrapperApi": False,
            "sectionIds": [
                "BOOK_IT_FLOATING_FOOTER", "POLICIES_DEFAULT",
                "EDUCATION_FOOTER_BANNER_MODAL", "BOOK_IT_SIDEBAR",
                "URGENCY_COMMITMENT_SIDEBAR", "BOOK_IT_NAV", "MESSAGE_BANNER",
                "HIGHLIGHTS_DEFAULT", "EDUCATION_FOOTER_BANNER",
                "URGENCY_COMMITMENT", "BOOK_IT_CALENDAR_SHEET",
                "CANCELLATION_POLICY_PICKER_MODAL",
            ],
            "checkIn": check_in.isoformat() if check_in else None,
            "checkOut": check_out.isoformat() if check_out else None,
        },
    }


def _review_variables(listing_id: str) -> dict[str, Any]:
    return {
        "id": base64.b64encode(f"StayListing:{listing_id}".encode()).decode(),
        "pdpReviewsRequest": {
            "fieldSelector": "for_p3_translation_only",
            "forPreview": False,
            "limit": 10,
            "offset": "0",
            "showingTranslationButton": False,
            "first": 10,
            "sortingPreference": "MOST_RECENT",
            "numberOfAdults": "1",
            "numberOfChildren": "0",
            "numberOfInfants": "0",
            "numberOfPets": "0",
            "after": None,
        },
    }


async def _resolve_public_api_key(client: httpx.AsyncClient) -> tuple[str, str]:
    global _AIRBNB_API_KEY_CACHE, _AIRBNB_REGION_CACHE
    configured = (os.getenv("AIRBNB_PUBLIC_API_KEY") or "").strip()
    now = time.monotonic()
    if (
        _AIRBNB_API_KEY_CACHE
        and now - _AIRBNB_API_KEY_CACHE[2] < 6 * 60 * 60
    ):
        return configured or _AIRBNB_API_KEY_CACHE[0], _AIRBNB_API_KEY_CACHE[1]
    if (
        configured
        and _AIRBNB_REGION_CACHE
        and now - _AIRBNB_REGION_CACHE[1] < 6 * 60 * 60
    ):
        return configured, _AIRBNB_REGION_CACHE[0]
    current_url = f"{_AIRBNB_DEFAULT_BASE_URL}/"
    response = None
    for redirect_count in range(6):
        response = await client.get(
            current_url,
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        if redirect_count == 5 or not response.headers.get("Location"):
            raise RuntimeError("public_redirect_limit")
        next_url = urljoin(current_url, response.headers["Location"])
        parsed = urlparse(next_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _AIRBNB_HOSTS:
            raise RuntimeError("public_redirect_rejected")
        current_url = next_url
    if response is None:
        raise RuntimeError("public_page_unavailable")
    if response.status_code >= 400:
        raise _AirbnbPublicHttpError(
            response.status_code,
            _retry_after_seconds(response.headers.get("Retry-After")),
        )
    match = _AIRBNB_API_KEY_RE.search(response.text)
    soup = BeautifulSoup(response.text, "html.parser")
    handoff_form = soup.find("form", action=True)
    payload_input = soup.find("input", attrs={"name": "payload", "value": True})
    handoff_action = str(handoff_form.get("action") or "").strip() if handoff_form else ""
    handoff_payload = str(payload_input.get("value") or "").strip() if payload_input else ""
    if handoff_action and handoff_payload:
        handoff_url = urljoin(str(response.url or current_url), handoff_action)
        parsed_handoff = urlparse(handoff_url)
        if parsed_handoff.scheme != "https" or (parsed_handoff.hostname or "").lower() not in _AIRBNB_HOSTS:
            raise RuntimeError("public_handoff_rejected")
        handoff_data = {"version": "1", "payload": handoff_payload}
        response = await client.post(
            handoff_url,
            data=handoff_data,
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        for redirect_count in range(6):
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            if redirect_count == 5 or not response.headers.get("Location"):
                raise RuntimeError("public_redirect_limit")
            next_url = urljoin(str(response.url or handoff_url), response.headers["Location"])
            parsed_next = urlparse(next_url)
            if parsed_next.scheme != "https" or (parsed_next.hostname or "").lower() not in _AIRBNB_HOSTS:
                raise RuntimeError("public_redirect_rejected")
            if response.status_code in {307, 308}:
                response = await client.post(
                    next_url,
                    data=handoff_data,
                    headers={"Accept": "text/html"},
                    follow_redirects=False,
                )
            else:
                response = await client.get(
                    next_url,
                    headers={"Accept": "text/html"},
                    follow_redirects=False,
                )
        if response.status_code >= 400:
            raise _AirbnbPublicHttpError(
                response.status_code,
                _retry_after_seconds(response.headers.get("Retry-After")),
            )
        regional_match = _AIRBNB_API_KEY_RE.search(response.text)
        if regional_match:
            match = regional_match
    if not match and not configured:
        raise RuntimeError("public_api_key_unavailable")
    response_url = urlparse(str(response.url or current_url))
    if response_url.scheme != "https" or (response_url.hostname or "").lower() not in _AIRBNB_HOSTS:
        raise RuntimeError("public_handoff_rejected")
    base_url = f"https://{response_url.hostname}"
    _AIRBNB_REGION_CACHE = (base_url, now)
    if configured:
        return configured, base_url
    assert match is not None
    _AIRBNB_API_KEY_CACHE = (match.group(1), base_url, now)
    return _AIRBNB_API_KEY_CACHE[0], _AIRBNB_API_KEY_CACHE[1]


async def _fetch_public_listing_page(
    client: httpx.AsyncClient,
    listing_id: str,
    *,
    base_url: str = _AIRBNB_DEFAULT_BASE_URL,
) -> dict[str, Any]:
    parsed_base = urlparse(base_url)
    if parsed_base.scheme != "https" or (parsed_base.hostname or "").lower() not in _AIRBNB_HOSTS:
        raise RuntimeError("public_redirect_rejected")
    current_url = f"{base_url.rstrip('/')}/rooms/{listing_id}"
    response = None
    for redirect_count in range(6):
        response = await client.get(
            current_url,
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        if redirect_count == 5 or not response.headers.get("Location"):
            raise RuntimeError("public_redirect_limit")
        next_url = urljoin(current_url, response.headers["Location"])
        parsed = urlparse(next_url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() not in _AIRBNB_HOSTS
            or _LISTING_PATH_RE.fullmatch(parsed.path) is None
            or _LISTING_PATH_RE.fullmatch(parsed.path).group(1) != listing_id
        ):
            raise RuntimeError("public_redirect_rejected")
        current_url = next_url
    if response is None:
        raise RuntimeError("listing_page_unavailable")
    if response.status_code >= 400:
        raise _AirbnbPublicHttpError(
            response.status_code,
            _retry_after_seconds(response.headers.get("Retry-After")),
        )

    soup = BeautifulSoup(response.text, "html.parser")
    deferred_state = soup.select_one("#data-deferred-state-0")
    if deferred_state is None:
        raise RuntimeError("listing_page_payload_unavailable")
    try:
        state = json.loads(deferred_state.get_text())
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("listing_page_payload_unavailable") from exc
    niobe_rows = state.get("niobeClientData") if isinstance(state, dict) else None
    if isinstance(niobe_rows, list):
        for row in niobe_rows[:100]:
            if not isinstance(row, list) or len(row) < 2 or not isinstance(row[1], dict):
                continue
            if _graphql_sections(row[1]):
                return row[1]
    raise RuntimeError("listing_page_payload_unavailable")


class AirbnbCollector(ProfileOnlyCollector):
    SOURCE_NAME = "airbnb"
    PROFILE_TABLE = "airbnb_profiles"

    async def probe_profile(self, username: str) -> ProfileResult:
        # Airbnb uses numeric `/users/show/` IDs and slug `/users/profile/` routes.
        route = "show" if username.isdigit() else "profile"
        url = f"https://www.airbnb.com/users/{route}/{quote(username, safe='')}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(url, headers=headers, timeout=15.0)
        except Exception as exc:
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{type(exc).__name__}")

        if r.status_code == 404:
            return ProfileResult(exists=False, username=username, fields={})
        if r.status_code >= 400:
            retry_after = _retry_after_seconds(r.headers.get("Retry-After"))
            error = f"http:{r.status_code}"
            if retry_after is not None:
                error += f":retry_after:{retry_after}"
            return ProfileResult(exists=False, username=username, fields={},
                                 error=error)

        html = r.text
        first_m = _DISPLAY_RE.search(html)
        if not first_m:
            profile = _parse_airbnb_host_profile_page(html, username)
            if profile:
                host_since = profile.get("host_since")
                try:
                    host_since = date.fromisoformat(host_since[:10]) if isinstance(host_since, str) else host_since
                except ValueError:
                    pass
                return ProfileResult(
                    exists=True,
                    username=username,
                    fields={
                        "display_name": profile.get("display_name"),
                        "avatar_url": profile.get("avatar_url"),
                        "host_since": host_since,
                        "superhost": None,
                        "location_city": None,
                        "properties_count": None,
                        "reviews_count": profile.get("reviews_count"),
                        "bio": profile.get("bio"),
                    },
                )
            # Airbnb can return a successful app shell or challenge page without
            # profile data. Do not mark that response as a confirmed 404.
            return ProfileResult(
                exists=False,
                username=username,
                fields={},
                error="profile_payload_unavailable",
            )

        last_m = _LAST_RE.search(html)
        avatar_m = _AVATAR_RE.search(html)
        host_since_m = _HOST_SINCE_RE.search(html)
        superhost_m = _SUPERHOST_RE.search(html)
        location_m = _LOCATION_RE.search(html)
        listings_m = _LISTINGS_RE.search(html)
        reviews_m = _REVIEWS_RE.search(html)
        bio_m = _PROFILE_BIO_RE.search(html)

        display_name = _decode_json_str(first_m.group(1))
        if last_m:
            display_name = f"{display_name} {_decode_json_str(last_m.group(1))}"

        host_since_parsed: date | None = None
        if host_since_m:
            try:
                host_since_parsed = date.fromisoformat(host_since_m.group(1))
            except ValueError:
                host_since_parsed = None

        return ProfileResult(
            exists=True,
            username=username,
            fields={
                "display_name": display_name,
                "avatar_url": _decode_json_str(avatar_m.group(1)) if avatar_m else None,
                "host_since": host_since_parsed,
                "superhost": (superhost_m.group(1) == "true") if superhost_m else None,
                "location_city": _decode_json_str(location_m.group(1)) if location_m else None,
                "properties_count": int(listings_m.group(1)) if listings_m else None,
                "reviews_count": int(reviews_m.group(1)) if reviews_m else None,
                "bio": _airbnb_text(_decode_json_str(bio_m.group(1))) if bio_m else None,
            },
        )

    @staticmethod
    def _target_id(target_url: str) -> tuple[str, str] | None:
        parsed = urlparse(target_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _AIRBNB_HOSTS:
            return None
        listing_match = _LISTING_PATH_RE.fullmatch(parsed.path)
        if listing_match:
            return "listing", listing_match.group(1)
        host_match = _HOST_PATH_RE.fullmatch(parsed.path)
        if host_match:
            return "host", host_match.group(1)
        profile_match = _HOST_PROFILE_PATH_RE.fullmatch(parsed.path)
        if profile_match:
            return "host", profile_match.group(1)
        return None

    @staticmethod
    async def _graphql(
        client: httpx.AsyncClient,
        operation: str,
        query_hash: str,
        variables: dict[str, Any],
        *,
        base_url: str = _AIRBNB_DEFAULT_BASE_URL,
        trailing_slash: bool = False,
    ) -> dict[str, Any]:
        params = {
            "operationName": operation,
            "locale": "en",
            "currency": "USD",
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": query_hash}},
                separators=(",", ":"),
            ),
        }
        parsed_base = urlparse(base_url)
        if parsed_base.scheme != "https" or (parsed_base.hostname or "").lower() not in _AIRBNB_HOSTS:
            raise RuntimeError("public_redirect_rejected")
        operation_url = f"{base_url.rstrip('/')}/api/v3/{operation}/{query_hash}"
        if trailing_slash:
            operation_url += "/"
        response = await client.get(operation_url, params=params)
        if response.status_code >= 400:
            raise _AirbnbPublicHttpError(
                response.status_code,
                _retry_after_seconds(response.headers.get("Retry-After")),
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("graphql_payload_unavailable") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("graphql_payload_unavailable")
        if payload.get("errors"):
            raise RuntimeError("graphql_query_failed")
        if not isinstance(payload.get("data"), dict):
            raise RuntimeError("graphql_payload_unavailable")
        return payload

    async def _fetch_host_profile(self, host_id: str, profile_url: str) -> dict[str, Any]:
        if not host_id.isdigit():
            result = await self.probe_profile(host_id)
            if not result.exists:
                status = re.fullmatch(r"http:(\d{3})(?::retry_after:(\d+))?", result.error or "")
                if status and int(status.group(1)) in {403, 429}:
                    raise _AirbnbPublicHttpError(
                        int(status.group(1)),
                        int(status.group(2)) if status.group(2) else None,
                    )
                raise RuntimeError(
                    "host_profile_payload_unavailable"
                    if result.error == "profile_payload_unavailable"
                    else "host_profile_unavailable"
                )
            fields = result.fields
            host_since = fields.get("host_since")
            return {
                "host_id": host_id,
                "display_name": fields.get("display_name"),
                "profile_url": profile_url,
                "avatar_url": _https_value(fields.get("avatar_url")),
                "rating": None,
                "reviews_count": _number_value(fields.get("reviews_count"), integer=True),
                "host_since": host_since.isoformat() if hasattr(host_since, "isoformat") else host_since,
                "bio": _airbnb_text(fields.get("bio")),
                "response_rate": None,
            }

        headers = {
            "X-Airbnb-GraphQL-Platform-Client": "minimalist-niobe",
            "X-Airbnb-GraphQL-Platform": "web",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(20.0), follow_redirects=False,
        ) as client:
            api_key, base_url = await _resolve_public_api_key(client)
            client.headers["X-Airbnb-API-Key"] = api_key
            encoded_user_id = base64.b64encode(f"User:{host_id}".encode()).decode()
            try:
                payload = await self._graphql(
                    client,
                    "GetUserProfile",
                    _USER_PROFILE_HASH,
                    {
                        "userId": encoded_user_id,
                        "isPassportStampsEnabled": True,
                        "mockIdentifier": None,
                        "fetchCombinedSportsAndInterests": True,
                    },
                    base_url=base_url,
                )
            except RuntimeError as graphql_error:
                # Airbnb sometimes serves the public host page even when its
                # persisted GraphQL operation returns an application error.
                # Reuse the existing public profile parser as a bounded
                # fallback; HTTP 403/429 still preserve their retry windows.
                if str(graphql_error) not in {
                    "graphql_query_failed",
                    "graphql_payload_unavailable",
                }:
                    raise
                logger.info(
                    "Airbnb host GraphQL returned %s; trying public profile page",
                    str(graphql_error),
                )
                probed = await self.probe_profile(host_id)
                if not probed.exists:
                    logger.info(
                        "Airbnb public host-profile fallback unavailable (%s)",
                        probed.error or "profile_payload_unavailable",
                    )
                    status = re.fullmatch(
                        r"http:(\d{3})(?::retry_after:(\d+))?",
                        probed.error or "",
                    )
                    if status and int(status.group(1)) in {403, 429}:
                        raise _AirbnbPublicHttpError(
                            int(status.group(1)),
                            int(status.group(2)) if status.group(2) else None,
                        ) from graphql_error
                    raise graphql_error
                fields = probed.fields
                host_since = fields.get("host_since")
                fallback_profile = {
                    "host_id": host_id,
                    "display_name": fields.get("display_name"),
                    "profile_url": profile_url,
                    "avatar_url": _https_value(fields.get("avatar_url")),
                    "rating": None,
                    "reviews_count": _number_value(
                        fields.get("reviews_count"), integer=True,
                    ),
                    "host_since": (
                        host_since.isoformat()
                        if hasattr(host_since, "isoformat")
                        else host_since
                    ),
                    "bio": _airbnb_text(fields.get("bio")),
                    "response_rate": None,
                }
                if not any(fallback_profile.get(key) for key in (
                    "display_name", "avatar_url", "rating", "reviews_count",
                    "host_since", "bio", "response_rate",
                )):
                    raise graphql_error
                return fallback_profile
        profile = _parse_airbnb_host_profile(payload, host_id)
        profile["profile_url"] = profile_url
        if not any(profile.get(key) for key in (
            "display_name", "avatar_url", "rating", "reviews_count", "host_since", "bio", "response_rate",
        )):
            raise RuntimeError("host_profile_payload_unavailable")
        return profile

    async def _fetch_listing_data(
        self, listing_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        headers = {
            "X-Airbnb-GraphQL-Platform-Client": "minimalist-niobe",
            "X-Airbnb-GraphQL-Platform": "web",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20.0), follow_redirects=False) as client:
            api_key, base_url = await _resolve_public_api_key(client)
            client.headers["X-Airbnb-API-Key"] = api_key
            today = datetime.now(timezone.utc).date()
            details_payload = await _fetch_public_listing_page(client, listing_id, base_url=base_url)
            listing = _parse_airbnb_listing(details_payload, listing_id)
            if listing is None:
                raise RuntimeError("listing_payload_unavailable")

            calendar_payload = await self._graphql(
                client,
                "PdpAvailabilityCalendar",
                _PDP_AVAILABILITY_HASH,
                {"request": {
                    "count": 12,
                    "listingId": listing_id,
                    "month": today.month,
                    "year": today.year,
                }},
                base_url=base_url,
                trailing_slash=True,
            )
            availability = _parse_airbnb_calendar(calendar_payload)
            price_range = _first_available_stay(availability, today)
            price_amount: float | None = None
            price_currency: str | None = None
            if price_range:
                price_payload = await self._graphql(
                    client,
                    "StaysPdpSections",
                    _PDP_SECTIONS_HASH,
                    _stays_detail_variables(
                        listing_id,
                        price_range[0],
                        price_range[1],
                    ),
                    base_url=base_url,
                )
                price_amount, price_currency = _parse_airbnb_price(price_payload)
            if price_amount is None and price_range:
                first_day = next((
                    row for row in availability
                    if row.get("date") == price_range[0].isoformat()
                ), None)
                if first_day:
                    price_amount, price_currency = _airbnb_money(
                        first_day.get("price"), first_day.get("currency"),
                    )
            listing.update({
                "price_amount": price_amount,
                "price_currency": price_currency,
                "price_check_in": price_range[0] if price_range else None,
                "price_check_out": price_range[1] if price_range else None,
            })

            reviews: list[dict[str, Any]] = []
            if listing["reviews_count"] != 0:
                review_payload = await self._graphql(
                    client,
                    "StaysPdpReviewsQuery",
                    _PDP_REVIEWS_HASH,
                    _review_variables(listing_id),
                    base_url=base_url,
                    trailing_slash=True,
                )
                reviews = _parse_airbnb_reviews(review_payload, listing_id)
        return listing, reviews, availability

    async def run_native_cycle(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "processed": 0, "listings": 0, "hosts": 0, "reviews": 0,
            "availability_days": 0,
        }
        if os.getenv("AIRBNB_NATIVE_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            summary["skipped"] = "disabled"
            return summary
        if self.pool is None:
            summary["skipped"] = "no_pool"
            return summary

        from src.collectors.native_platform_scrape import (
            claim_native_target,
            finish_native_target,
            seed_native_targets,
        )

        await seed_native_targets(self.pool, self.SOURCE_NAME)
        target = await claim_native_target(self.pool, self.SOURCE_NAME)
        if target is None:
            return summary
        summary["processed"] = 1
        try:
            parsed_target = self._target_id(str(target["target_url"]))
            if parsed_target is None:
                raise RuntimeError("unsupported_public_page")
            target_kind, target_value = parsed_target
            if target_kind == "host":
                host = await self._fetch_host_profile(
                    target_value,
                    str(target["target_url"]),
                )
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO airbnb_hosts
                            (host_id,display_name,profile_url,avatar_url,rating,reviews_count,
                             host_since,bio,response_rate,collected_at,metadata)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now(),$10::jsonb)
                        ON CONFLICT (host_id) DO UPDATE SET
                            display_name=COALESCE(EXCLUDED.display_name,airbnb_hosts.display_name),
                            profile_url=EXCLUDED.profile_url,
                            avatar_url=COALESCE(EXCLUDED.avatar_url,airbnb_hosts.avatar_url),
                            rating=COALESCE(EXCLUDED.rating,airbnb_hosts.rating),
                            reviews_count=COALESCE(EXCLUDED.reviews_count,airbnb_hosts.reviews_count),
                            host_since=COALESCE(EXCLUDED.host_since,airbnb_hosts.host_since),
                            bio=COALESCE(EXCLUDED.bio,airbnb_hosts.bio),
                            response_rate=COALESCE(EXCLUDED.response_rate,airbnb_hosts.response_rate),
                            collected_at=now(),metadata=airbnb_hosts.metadata||EXCLUDED.metadata
                        """,
                        host["host_id"], host["display_name"], host["profile_url"],
                        host["avatar_url"], host["rating"], host["reviews_count"],
                        host["host_since"], host["bio"], host["response_rate"],
                        json.dumps({"source": "airbnb_user_profile_graphql"}),
                    )
                await finish_native_target(
                    self.pool, self.SOURCE_NAME, int(target["id"]), success=True,
                )
                summary["hosts"] = 1
                return summary

            listing, reviews, availability = await self._fetch_listing_data(target_value)
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO airbnb_listings
                            (listing_id,host_id,host_name,host_profile_url,title,listing_url,location,
                             image_url,rating,reviews_count,property_type,bedrooms,bathrooms,max_guests,
                             description,photos,price_amount,price_currency,price_check_in,price_check_out,
                             amenities,availability,availability_checked_at,collected_at,metadata)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                                $16::jsonb,$17,$18,$19::date,$20::date,$21::jsonb,$22::jsonb,
                                CASE WHEN jsonb_array_length($22::jsonb)>0 THEN now() ELSE NULL END,
                                now(),$23::jsonb)
                        ON CONFLICT (listing_id) DO UPDATE SET
                            host_id=COALESCE(EXCLUDED.host_id,airbnb_listings.host_id),
                            host_name=COALESCE(EXCLUDED.host_name,airbnb_listings.host_name),
                            host_profile_url=COALESCE(EXCLUDED.host_profile_url,airbnb_listings.host_profile_url),
                            title=COALESCE(EXCLUDED.title,airbnb_listings.title),
                            listing_url=EXCLUDED.listing_url,
                            location=COALESCE(EXCLUDED.location,airbnb_listings.location),
                            image_url=COALESCE(EXCLUDED.image_url,airbnb_listings.image_url),
                            rating=COALESCE(EXCLUDED.rating,airbnb_listings.rating),
                            reviews_count=COALESCE(EXCLUDED.reviews_count,airbnb_listings.reviews_count),
                            property_type=COALESCE(EXCLUDED.property_type,airbnb_listings.property_type),
                            bedrooms=COALESCE(EXCLUDED.bedrooms,airbnb_listings.bedrooms),
                            bathrooms=COALESCE(EXCLUDED.bathrooms,airbnb_listings.bathrooms),
                            max_guests=COALESCE(EXCLUDED.max_guests,airbnb_listings.max_guests),
                            description=COALESCE(EXCLUDED.description,airbnb_listings.description),
                            photos=CASE WHEN jsonb_array_length(EXCLUDED.photos)>0
                                THEN EXCLUDED.photos ELSE airbnb_listings.photos END,
                            price_amount=COALESCE(EXCLUDED.price_amount,airbnb_listings.price_amount),
                            price_currency=COALESCE(EXCLUDED.price_currency,airbnb_listings.price_currency),
                            price_check_in=COALESCE(EXCLUDED.price_check_in,airbnb_listings.price_check_in),
                            price_check_out=COALESCE(EXCLUDED.price_check_out,airbnb_listings.price_check_out),
                            amenities=CASE WHEN jsonb_array_length(EXCLUDED.amenities)>0
                                THEN EXCLUDED.amenities ELSE airbnb_listings.amenities END,
                            availability=CASE WHEN jsonb_array_length(EXCLUDED.availability)>0
                                THEN EXCLUDED.availability ELSE airbnb_listings.availability END,
                            availability_checked_at=CASE
                                WHEN jsonb_array_length(EXCLUDED.availability)>0 THEN now()
                                ELSE airbnb_listings.availability_checked_at END,
                            collected_at=now(),
                            metadata=airbnb_listings.metadata||EXCLUDED.metadata
                        """,
                        listing["listing_id"], listing["host_id"], listing["host_name"],
                        listing["host_profile_url"], listing["title"], listing["url"],
                        listing["location"], listing["image_url"], listing["rating"],
                        listing["reviews_count"], listing["property_type"], listing["bedrooms"],
                        listing["bathrooms"], listing["max_guests"], listing["description"],
                        json.dumps(listing["photos"]), listing["price_amount"], listing["price_currency"],
                        listing["price_check_in"], listing["price_check_out"],
                        json.dumps(listing["amenities"]), json.dumps(availability),
                        json.dumps({"source": "airbnb_graphql"}),
                    )
                    if listing["host_id"]:
                        await conn.execute(
                            """
                            INSERT INTO airbnb_hosts
                                (host_id,display_name,profile_url,avatar_url,rating,reviews_count,
                                 host_since,bio,response_rate,collected_at,metadata)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now(),$10::jsonb)
                            ON CONFLICT (host_id) DO UPDATE SET
                                display_name=COALESCE(EXCLUDED.display_name,airbnb_hosts.display_name),
                                profile_url=EXCLUDED.profile_url,
                                avatar_url=COALESCE(EXCLUDED.avatar_url,airbnb_hosts.avatar_url),
                                rating=COALESCE(EXCLUDED.rating,airbnb_hosts.rating),
                                reviews_count=COALESCE(EXCLUDED.reviews_count,airbnb_hosts.reviews_count),
                                host_since=COALESCE(EXCLUDED.host_since,airbnb_hosts.host_since),
                                bio=COALESCE(EXCLUDED.bio,airbnb_hosts.bio),
                                response_rate=COALESCE(EXCLUDED.response_rate,airbnb_hosts.response_rate),
                                collected_at=now(),metadata=airbnb_hosts.metadata||EXCLUDED.metadata
                            """,
                            listing["host_id"], listing["host_name"], listing["host_profile_url"],
                            listing["host_avatar_url"], listing["host_rating"],
                            listing["host_reviews_count"], listing["host_since"],
                            listing["host_bio"], listing["host_response_rate"],
                            json.dumps({"source": "airbnb_graphql"}),
                        )
                    for review in reviews:
                        await conn.execute(
                            """
                            INSERT INTO airbnb_reviews
                                (review_id,listing_id,reviewer_id,review_date,rating,comments,
                                 language,collected_at,metadata)
                            VALUES ($1,$2,$3,$4::text::timestamptz,$5,$6,$7,now(),$8::jsonb)
                            ON CONFLICT (review_id) DO UPDATE SET
                                listing_id=EXCLUDED.listing_id,
                                reviewer_id=COALESCE(EXCLUDED.reviewer_id,airbnb_reviews.reviewer_id),
                                review_date=COALESCE(EXCLUDED.review_date,airbnb_reviews.review_date),
                                rating=COALESCE(EXCLUDED.rating,airbnb_reviews.rating),
                                comments=COALESCE(EXCLUDED.comments,airbnb_reviews.comments),
                                language=COALESCE(EXCLUDED.language,airbnb_reviews.language),
                                collected_at=now(),metadata=airbnb_reviews.metadata||EXCLUDED.metadata
                            """,
                            review["review_id"], review["listing_id"], review["reviewer_id"],
                            review["review_date"], review["rating"], review["comments"],
                            review["language"], json.dumps({"source": "airbnb_graphql"}),
                        )
            await finish_native_target(self.pool, self.SOURCE_NAME, int(target["id"]), success=True)
            summary.update(
                listings=1,
                hosts=1 if listing["host_id"] else 0,
                reviews=len(reviews),
                availability_days=len(availability),
            )
        except Exception as exc:
            attempts = int(target.get("attempts") or 1)
            exponential_retry = min(24 * 60 * 60, 15 * 60 * (2 ** min(max(attempts - 1, 0), 6)))
            retry_after_seconds = getattr(exc, "retry_after_seconds", None)
            if isinstance(exc, _AirbnbPublicHttpError) and exc.status_code == 429:
                retry_after_seconds = max(exponential_retry, int(retry_after_seconds or 0))
            elif isinstance(exc, _AirbnbPublicHttpError) and exc.status_code == 403:
                retry_after_seconds = max(24 * 60 * 60, int(retry_after_seconds or 0))
            elif retry_after_seconds is None:
                retry_after_seconds = max(exponential_retry, 2 * 60 * 60)
            error_code = _airbnb_error_code(exc)
            await finish_native_target(
                self.pool,
                self.SOURCE_NAME,
                int(target["id"]),
                success=False,
                error=error_code,
                retry_after_seconds=retry_after_seconds,
            )
            logger.warning("Airbnb GraphQL scrape failed: %s", error_code)
            summary["error"] = error_code
        return summary


__all__ = ["AirbnbCollector"]
