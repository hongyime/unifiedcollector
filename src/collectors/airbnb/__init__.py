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
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult

logger = logging.getLogger(__name__)

# Airbnb serializes user data inside a data-state JSON blob. Field names
# below match what the bookmarklet at tools.myosint.training reads.
_DISPLAY_RE = re.compile(r'"first_name"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_LAST_RE = re.compile(r'"last_name"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_AVATAR_RE = re.compile(r'"picture_url"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_HOST_SINCE_RE = re.compile(r'"created_at"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_SUPERHOST_RE = re.compile(r'"is_superhost"\s*:\s*(true|false)')
_LOCATION_RE = re.compile(r'"location"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_LISTINGS_RE = re.compile(r'"listings_count"\s*:\s*(\d+)')
_REVIEWS_RE = re.compile(r'"reviews_count"\s*:\s*(\d+)')

# Public persisted-query identifiers used by Airbnb's web client. The API key
# is also public web-client configuration; deployments may override it.
_AIRBNB_PUBLIC_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"
_PDP_SECTIONS_HASH = "15a1a5ea715709931e72456c87780f01d49acb8e8d98d9f8c99f1cdda528055b"
_PDP_REVIEWS_HASH = "2ed951bfedf71b87d9d30e24a419e15517af9fbed7ac560a8d1cc7feadfa22e6"
_AIRBNB_HOSTS = frozenset({
    "airbnb.com", "www.airbnb.com", "airbnb.com.sg", "www.airbnb.com.sg",
    "airbnb.co.uk", "www.airbnb.co.uk", "airbnb.co.kr", "www.airbnb.co.kr",
})
_LISTING_PATH_RE = re.compile(r"^/rooms/(\d{1,24})/?$")
_HOST_PATH_RE = re.compile(r"^/users/show/(\d{1,20})/?$")


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
    sections = detail.get("sections")
    result: dict[str, dict[str, Any]] = {}
    for row in _walk_dicts(sections):
        section_id = row.get("sectionId")
        if isinstance(section_id, str):
            result[section_id] = row
    return result


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
    host_name = _text_value(_find_scalar(host_card, ("displayName", "hostName", "name")), 300)
    host_avatar = _https_value(_find_scalar(host_card, ("avatarUrl", "profilePicture", "pictureUrl", "avatar")))
    host_rating = _number_value(_find_scalar(host_card, ("ratingAverage", "averageRating", "rating")))
    host_reviews = _number_value(
        _find_scalar(host_card, ("reviewsCount", "reviewCount")), integer=True,
    )
    host_since = _text_value(_find_scalar(host_card, ("timeAsHost", "hostSince", "createdAt")), 64)

    listing_sections = {key: value for key, value in sections.items() if key != "MEET_YOUR_HOST"}
    image_url = _https_value(_find_scalar(listing_sections, ("imageUrl", "pictureUrl", "baseUrl", "image")))
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
        "title": title,
        "url": f"https://www.airbnb.com/rooms/{listing_id}",
        "location": location,
        "image_url": image_url,
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


class AirbnbCollector(ProfileOnlyCollector):
    SOURCE_NAME = "airbnb"
    PROFILE_TABLE = "airbnb_profiles"

    async def probe_profile(self, username: str) -> ProfileResult:
        # Airbnb profiles are addressed by their numeric user ID.
        url = f"https://www.airbnb.com/users/show/{username}"
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
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{r.status_code}")

        html = r.text
        first_m = _DISPLAY_RE.search(html)
        if not first_m:
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
        return None

    @staticmethod
    async def _graphql(
        client: httpx.AsyncClient,
        operation: str,
        query_hash: str,
        variables: dict[str, Any],
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
        response = await client.get(f"https://www.airbnb.com/api/v3/{operation}/{query_hash}", params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"public_http_{response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("graphql_payload_unavailable")
        if payload.get("errors"):
            raise RuntimeError("graphql_query_failed")
        if not isinstance(payload.get("data"), dict):
            raise RuntimeError("graphql_payload_unavailable")
        return payload

    async def _fetch_listing_data(self, listing_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        api_key = (os.getenv("AIRBNB_PUBLIC_API_KEY") or _AIRBNB_PUBLIC_API_KEY).strip()
        headers = {
            "X-Airbnb-API-Key": api_key,
            "X-Airbnb-GraphQL-Platform-Client": "minimalist-niobe",
            "X-Airbnb-GraphQL-Platform": "web",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }
        encoded_id = base64.b64encode(f"StayListing:{listing_id}".encode()).decode()
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20.0), follow_redirects=False) as client:
            sections = await self._graphql(
                client,
                "StaysPdpSections",
                _PDP_SECTIONS_HASH,
                {
                    "id": encoded_id,
                    "pdpSectionsRequest": {
                        "adults": "1",
                        "layouts": ["SIDEBAR", "SINGLE_COLUMN"],
                        "checkIn": None,
                        "checkOut": None,
                        "children": "0",
                        "infants": "0",
                        "pets": 0,
                        "useContextualUser": False,
                    },
                },
            )
            listing = _parse_airbnb_listing(sections, listing_id)
            if listing is None:
                raise RuntimeError("listing_payload_unavailable")

            reviews: list[dict[str, Any]] = []
            if listing["reviews_count"] != 0:
                review_payload = await self._graphql(
                    client,
                    "StaysPdpReviewsQuery",
                    _PDP_REVIEWS_HASH,
                    {
                        "id": encoded_id,
                        "pdpReviewsRequest": {
                            "fieldSelector": "for_p3_translation_only",
                            "forPreview": False,
                            "limit": 10,
                            "offset": "0",
                            "showingTranslationButton": False,
                            "first": 10,
                            "sortingPreference": "BEST_QUALITY",
                        },
                    },
                )
                reviews = _parse_airbnb_reviews(review_payload, listing_id)
        return listing, reviews

    async def run_native_cycle(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"processed": 0, "listings": 0, "hosts": 0, "reviews": 0}
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
                async with self.pool.acquire() as conn:
                    listing_id = await conn.fetchval(
                        """
                        SELECT listing_id FROM airbnb_listings
                         WHERE host_id=$1
                         ORDER BY collected_at DESC, listing_id
                         LIMIT 1
                        """,
                        target_value,
                    )
                if not listing_id:
                    raise RuntimeError("host_listing_unavailable")
                target_value = str(listing_id)

            listing, reviews = await self._fetch_listing_data(target_value)
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO airbnb_listings
                            (listing_id,host_id,host_name,host_profile_url,title,listing_url,location,
                             image_url,rating,reviews_count,property_type,bedrooms,bathrooms,max_guests,
                             collected_at,metadata)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,now(),$15::jsonb)
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
                            collected_at=now(),metadata=airbnb_listings.metadata||EXCLUDED.metadata
                        """,
                        listing["listing_id"], listing["host_id"], listing["host_name"],
                        listing["host_profile_url"], listing["title"], listing["url"],
                        listing["location"], listing["image_url"], listing["rating"],
                        listing["reviews_count"], listing["property_type"], listing["bedrooms"],
                        listing["bathrooms"], listing["max_guests"],
                        json.dumps({"source": "airbnb_graphql"}),
                    )
                    if listing["host_id"]:
                        await conn.execute(
                            """
                            INSERT INTO airbnb_hosts
                                (host_id,display_name,profile_url,avatar_url,rating,reviews_count,
                                 host_since,collected_at,metadata)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,now(),$8::jsonb)
                            ON CONFLICT (host_id) DO UPDATE SET
                                display_name=COALESCE(EXCLUDED.display_name,airbnb_hosts.display_name),
                                profile_url=EXCLUDED.profile_url,
                                avatar_url=COALESCE(EXCLUDED.avatar_url,airbnb_hosts.avatar_url),
                                rating=COALESCE(EXCLUDED.rating,airbnb_hosts.rating),
                                reviews_count=COALESCE(EXCLUDED.reviews_count,airbnb_hosts.reviews_count),
                                host_since=COALESCE(EXCLUDED.host_since,airbnb_hosts.host_since),
                                collected_at=now(),metadata=airbnb_hosts.metadata||EXCLUDED.metadata
                            """,
                            listing["host_id"], listing["host_name"], listing["host_profile_url"],
                            listing["host_avatar_url"], listing["host_rating"],
                            listing["host_reviews_count"], listing["host_since"],
                            json.dumps({"source": "airbnb_graphql"}),
                        )
                    for review in reviews:
                        await conn.execute(
                            """
                            INSERT INTO airbnb_reviews
                                (review_id,listing_id,reviewer_id,review_date,rating,comments,
                                 language,collected_at,metadata)
                            VALUES ($1,$2,$3,$4::timestamptz,$5,$6,$7,now(),$8::jsonb)
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
            summary.update(listings=1, hosts=1 if listing["host_id"] else 0, reviews=len(reviews))
        except Exception as exc:
            message = str(exc)
            retry_hours = 24 if any(token in message for token in ("429", "403", "unavailable")) else 6
            await finish_native_target(
                self.pool,
                self.SOURCE_NAME,
                int(target["id"]),
                success=False,
                error=message if message.startswith("public_http_") else type(exc).__name__.lower(),
                retry_after_hours=retry_hours,
            )
            logger.warning("Airbnb GraphQL scrape failed: %s", type(exc).__name__)
            summary["error"] = type(exc).__name__
        return summary


__all__ = ["AirbnbCollector"]
