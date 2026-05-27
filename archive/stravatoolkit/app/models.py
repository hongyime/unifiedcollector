from __future__ import annotations

from pydantic import BaseModel


class StatusResponse(BaseModel):
    athlete_count: int
    activity_count: int
    follow_roster_size: int
    tracked_roster_size: int
    backfill_completed: int
    backfill_pending: int
    backfill_degraded: int
    backfill_needs_endpoint: int
    last_successful_sync_date: str | None


class TripResponse(BaseModel):
    activity_id: int
    athlete_id: int
    athlete_name: str
    activity_name: str | None
    sport_type: str
    color: list[int]
    athlete_avatar_url: str | None
    start_unix: int | None
    end_unix: int | None
    privacy_zone_start: bool
    privacy_zone_end: bool
    truncation_point_start: list[float] | None
    truncation_point_end: list[float] | None
    stream_status: str
    path: list[list[float | int]]


class DayPlaybackResponse(BaseModel):
    date: str
    timezone: str
    day_start_unix: int
    day_end_unix: int
    athlete_count: int
    trips: list[TripResponse]


class AthleteSummaryResponse(BaseModel):
    athlete_id: int
    name: str
    avatar_url: str | None
    is_following: bool
    is_tracked: bool
    activity_count: int
    backfill_status: str
    backfill_completed_at: str | None
    backfill_oldest_seen_utc: str | None
    backfill_last_issue_code: str | None
    backfill_last_issue_message: str | None
    backfill_last_issue_at: str | None
    color: list[int]


class AthleteListResponse(BaseModel):
    athletes: list[AthleteSummaryResponse]


class AthleteActivityResponse(BaseModel):
    activity_id: int
    activity_name: str | None
    sport_type: str
    calendar_date: str
    start_date_utc: str
    source: str
    stream_status: str


class AthleteDetailResponse(AthleteSummaryResponse):
    recent_activities: list[AthleteActivityResponse]


class AthleteRouteActivityResponse(BaseModel):
    activity_id: int
    activity_name: str | None
    sport_type: str
    calendar_date: str
    start_unix: int | None
    end_unix: int | None
    stream_status: str
    color: list[int]
    path: list[list[float | int]]


class AthleteRoutesResponse(BaseModel):
    athlete_id: int
    name: str
    activity_count: int
    routes: list[AthleteRouteActivityResponse]


class CoverageMonthResponse(BaseModel):
    month: str
    activity_count: int
    athlete_count: int
    ready_count: int


class CoverageYearResponse(BaseModel):
    year: str
    activity_count: int
    athlete_count: int
    months: list[CoverageMonthResponse]


class BackfillCoverageResponse(BaseModel):
    year_count: int
    month_count: int
    activity_count: int
    ready_count: int
    years: list[CoverageYearResponse]


class BackfillRunResponse(BaseModel):
    running: bool
    pid: int | None
    started_at: str | None
    log_path: str | None
    command: list[str]
