"""Unit tests for the API-layer primitives (docs/04 §2: TC-U-11, TC-U-18, TC-U-25).

TC-U-11 — bounds validation matrix (V-01..V-16 samples): each violation raises
a matching field error; valid edge values pass.
TC-U-18 — idempotent region id: same spec with shuffled keys / float noise
(17.3850 vs 17.385) yields an identical ``_id`` (canonicalization, DEC-23).
TC-U-25 — rate limiter bucket: 61 requests against a fake clock — first 60
pass, the 61st is refused with a Retry-After.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.core.ratelimit import RateLimiter, classify
from app.models.api import (
    AnalysisQuery,
    BboxBody,
    EventsQuery,
    InspectQuery,
    PointBody,
    RegionCreateBody,
    SearchQuery,
    current_year,
    last_full_year,
)
from app.models.region import (
    Point,
    RegionSpec,
    bbox_spec,
    point_spec,
    region_id_from_spec,
)

# --- TC-U-18: canonical / idempotent region id -------------------------------


def test_same_spec_same_id() -> None:
    a = point_spec(17.3850, 78.4867)
    b = point_spec(17.3850, 78.4867)
    assert region_id_from_spec(a) == region_id_from_spec(b)


def test_float_noise_same_id() -> None:
    """17.3850 vs 17.385 must not mint a second region (TC-U-18)."""
    a = point_spec(17.3850, 78.4867)
    b = point_spec(17.385, 78.48670)
    assert region_id_from_spec(a) == region_id_from_spec(b)


def test_shuffled_key_order_same_id() -> None:
    """dict key order is irrelevant — sort_keys=True in the canonical dump (§6.1)."""

    def make(mode: str, pts: list, bbox) -> str:
        payload: dict = {"points": pts, "mode": mode}  # deliberately shuffled
        if bbox is not None:
            payload["bbox"] = bbox
        spec = RegionSpec.model_validate(payload)
        return region_id_from_spec(spec)

    pts = [{"lat": 17.385, "lon": 78.4867}]
    assert make("point", pts, None) == region_id_from_spec(point_spec(17.3850, 78.4867))


def test_grid_points_are_deterministic() -> None:
    """Same bbox always yields the same 3×3 grid in row-major order (§5.2)."""
    bbox = [17.0, 78.0, 18.0, 79.0]
    assert [p.model_dump() for p in bbox_spec(bbox).points] == [
        p.model_dump() for p in bbox_spec(bbox).points
    ]
    assert len(bbox_spec(bbox).points) == 9


def test_different_point_different_id() -> None:
    a = point_spec(17.3850, 78.4867)
    b = point_spec(17.3851, 78.4867)  # differs at the 4th decimal — above noise floor
    assert region_id_from_spec(a) != region_id_from_spec(b)


def test_point_vs_grid_different_id() -> None:
    p = point_spec(17.0, 78.0)
    g = bbox_spec([16.5, 77.5, 17.5, 78.5])
    assert region_id_from_spec(p) != region_id_from_spec(g)


def test_id_format() -> None:
    rid = region_id_from_spec(point_spec(0.0, 0.0))
    assert rid.startswith("r_")
    assert len(rid) == 2 + 16
    assert all(c in "0123456789abcdef" for c in rid[2:])


def test_canonical_roundtrip_is_stable_json() -> None:
    """canonical() is deterministic — the same dict dumps to the same bytes."""
    spec = bbox_spec([10.0, 70.0, 12.0, 72.0])
    raw1 = json.dumps(spec.canonical(), sort_keys=True, separators=(",", ":"))
    raw2 = json.dumps(spec.canonical(), sort_keys=True, separators=(",", ":"))
    assert raw1 == raw2


# --- TC-U-11: bounds validation matrix (V-01..V-16) --------------------------
#
# Single-field violations surface as pydantic ValidationError (FastAPI maps
# these to 422 VALIDATION_ERROR with details.field); cross-field rules raise
# ApiError from validate_rules() with an explicit details.field.


def _valid_query(**overrides) -> AnalysisQuery:
    base = {
        "region_id": "r_0123456789abcdef",
        "start_year": 1990,
        "end_year": 2020,
    }
    base.update(overrides)
    return AnalysisQuery(**base)


class TestAnalysisQueryBounds:
    def test_valid_edge_passes(self) -> None:
        q = _valid_query(start_year=1950, end_year=last_full_year())
        q.validate_rules()
        assert q.range["start_year"] == 1950

    def test_v01_start_after_end(self) -> None:
        q = _valid_query(start_year=2000, end_year=1990)
        with pytest.raises(ApiError) as exc:
            q.validate_rules()
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.details["field"] == "start_year"

    def test_v02_start_below_1950(self) -> None:
        with pytest.raises(ValidationError):
            _valid_query(start_year=1949)

    def test_v02_end_above_last_full_year(self) -> None:
        q = _valid_query(end_year=current_year() + 1)
        with pytest.raises(ApiError) as exc:
            q.validate_rules()
        assert exc.value.details["field"] == "end_year"

    def test_v02_include_current_year_allows_this_year(self) -> None:
        q = _valid_query(end_year=current_year(), include_current_year=True)
        q.validate_rules()

    def test_v04_unknown_hazard(self) -> None:
        q = _valid_query(hazard="asteroid")
        with pytest.raises(ApiError) as exc:
            q.validate_rules()
        assert exc.value.details["field"] == "hazard"

    def test_v05_bad_region_id(self) -> None:
        for bad in ("", "r_short", "x_0123456789abcdef", "r_0123456789ABCDEF", "abc"):
            with pytest.raises(ValidationError):
                _valid_query(region_id=bad)

    def test_v13_bad_method(self) -> None:
        with pytest.raises(ValidationError):
            _valid_query(method="bayes")


class TestEventsQueryBounds:
    def test_v10_page_and_page_size(self) -> None:
        with pytest.raises(ValidationError):
            EventsQuery(region_id="r_0123456789abcdef", page=0)
        with pytest.raises(ValidationError):
            EventsQuery(region_id="r_0123456789abcdef", page_size=0)
        with pytest.raises(ValidationError):
            EventsQuery(region_id="r_0123456789abcdef", page_size=101)
        ok = EventsQuery(region_id="r_0123456789abcdef", page=1, page_size=100)
        assert ok.page_size == 100

    def test_v11_min_duration(self) -> None:
        with pytest.raises(ValidationError):
            EventsQuery(region_id="r_0123456789abcdef", min_duration=0)
        assert EventsQuery(region_id="r_0123456789abcdef", min_duration=1).min_duration == 1

    def test_v12_bad_sort(self) -> None:
        with pytest.raises(ValidationError):
            EventsQuery(region_id="r_0123456789abcdef", sort="duration:desc")
        assert EventsQuery(region_id="r_0123456789abcdef", sort="start_date:asc").sort.endswith(
            ":asc"
        )


class TestInspectQueryBounds:
    def test_bad_date_format(self) -> None:
        for bad in ("", "2020-1-1", "not-a-date", "20201301"):
            with pytest.raises(ValidationError):
                InspectQuery(region_id="r_0123456789abcdef", start_date=bad)

    def test_valid_date_passes(self) -> None:
        q = InspectQuery(region_id="r_0123456789abcdef", start_date="2020-06-15")
        q.validate_rules()

    def test_unknown_hazard(self) -> None:
        q = InspectQuery(region_id="r_0123456789abcdef", start_date="2020-06-15", hazard="flood")
        with pytest.raises(ApiError) as exc:
            q.validate_rules()
        assert exc.value.details["field"] == "hazard"


class TestPointBodyBounds:
    def test_v07_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            PointBody(lat=91, lon=0)
        with pytest.raises(ValidationError):
            PointBody(lat=0, lon=-181)
        with pytest.raises(ValidationError):
            PointBody(lat=-91, lon=181)

    def test_v07_edge_passes(self) -> None:
        assert PointBody(lat=90, lon=180).lat == 90
        assert PointBody(lat=-90, lon=-180).lon == -180


class TestBboxBounds:
    def test_v16_antimeridian(self) -> None:
        """min_lon > max_lon is the documented antimeridian limitation."""
        with pytest.raises(ApiError) as exc:
            BboxBody(min_lat=10, min_lon=170, max_lat=11, max_lon=-170).validate_rules()
        assert exc.value.details["field"] == "bbox"
        assert "antimeridian" in exc.value.message

    def test_v06_min_ge_max_lat(self) -> None:
        with pytest.raises(ApiError) as exc:
            BboxBody(min_lat=11, min_lon=10, max_lat=11, max_lon=11).validate_rules()
        assert "min_lat" in exc.value.message

    def test_v06_span_over_10(self) -> None:
        with pytest.raises(ApiError) as exc:
            BboxBody(min_lat=0, min_lon=0, max_lat=11, max_lon=1).validate_rules()
        assert "10 degrees" in exc.value.message

    def test_v06_area_over_25(self) -> None:
        with pytest.raises(ApiError) as exc:
            BboxBody(min_lat=0, min_lon=0, max_lat=6, max_lon=6).validate_rules()
        assert "25" in exc.value.message

    def test_valid_bbox_passes(self) -> None:
        box = [17.0, 78.0, 18.0, 79.0]
        assert BboxBody(min_lat=17, min_lon=78, max_lat=18, max_lon=79).validate_rules() == box


class TestRegionCreateBody:
    def test_v08_name_control_chars(self) -> None:
        with pytest.raises(ValidationError):
            RegionCreateBody(kind="city", name="a\x00b", point=PointBody(lat=1, lon=1))

    def test_kind_city_requires_point(self) -> None:
        with pytest.raises(ApiError) as exc:
            RegionCreateBody(kind="city").validate_rules()
        assert exc.value.details["field"] == "point"

    def test_kind_state_requires_bbox(self) -> None:
        with pytest.raises(ApiError) as exc:
            RegionCreateBody(kind="state").validate_rules()
        assert exc.value.details["field"] == "bbox"

    def test_kind_required_without_preset(self) -> None:
        with pytest.raises(ApiError) as exc:
            RegionCreateBody().validate_rules()
        assert exc.value.details["field"] == "kind"

    def test_preset_short_circuits(self) -> None:
        body = RegionCreateBody(preset_id="city_delhi")
        assert body.validate_rules() is body

    def test_valid_city(self) -> None:
        body = RegionCreateBody(
            kind="city", name="Hyderabad", point=PointBody(lat=17.385, lon=78.4867)
        )
        body.validate_rules()

    def test_v08_name_stripped(self) -> None:
        body = RegionCreateBody(kind="city", name="  Hyderabad  ", point=PointBody(lat=1, lon=1))
        assert body.name == "Hyderabad"

    def test_v08_name_over_80_rejected_by_field_constraint(self) -> None:
        with pytest.raises(ValidationError):
            RegionCreateBody(kind="city", name="x" * 81, point=PointBody(lat=1, lon=1))


class TestSearchQuery:
    def test_v09_too_short(self) -> None:
        with pytest.raises(ValidationError):
            SearchQuery(q="a")

    def test_v09_too_long(self) -> None:
        with pytest.raises(ValidationError):
            SearchQuery(q="x" * 81)

    def test_v09_control_chars(self) -> None:
        with pytest.raises(ValidationError):
            SearchQuery(q="del\x1fhi")

    def test_v09_valid(self) -> None:
        assert SearchQuery(q="  Hyderabad  ").q == "Hyderabad"


# --- TC-U-25: rate limiter bucket against a fake clock -----------------------


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_tc_u_25_sixty_pass_sixty_first_429() -> None:
    """First 60 pass; the 61st is refused with Retry-After (docs/04 TC-U-25)."""
    clock = FakeClock()
    limiter = RateLimiter({"analysis": 60}, clock=clock)

    for i in range(60):
        decision = limiter.check("10.0.0.1", "analysis")
        assert decision.allowed, f"request {i + 1} should pass"
        assert decision.limit == 60
        assert decision.remaining == 59 - i

    refused = limiter.check("10.0.0.1", "analysis")
    assert not refused.allowed
    assert refused.remaining == 0
    assert refused.retry_after_seconds is not None
    assert refused.retry_after_seconds >= 1


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter({"analysis": 60}, clock=clock)
    for _ in range(60):
        assert limiter.check("10.0.0.1", "analysis").allowed
    assert not limiter.check("10.0.0.1", "analysis").allowed

    # 1 token refills every 60/60 = 1 s at limit=60.
    clock.advance(1.0)
    assert limiter.check("10.0.0.1", "analysis").allowed
    assert not limiter.check("10.0.0.1", "analysis").allowed

    # Fully refill.
    clock.advance(120.0)
    decision = limiter.check("10.0.0.1", "analysis")
    assert decision.allowed
    assert decision.remaining == 59


def test_buckets_are_per_ip_and_per_class() -> None:
    clock = FakeClock()
    limiter = RateLimiter({"analysis": 2, "region": 1}, clock=clock)
    assert limiter.check("10.0.0.1", "analysis").allowed
    assert limiter.check("10.0.0.1", "analysis").allowed
    assert not limiter.check("10.0.0.1", "analysis").allowed
    # Different IP is independent.
    assert limiter.check("10.0.0.2", "analysis").allowed
    # Different class for the same IP is independent.
    assert limiter.check("10.0.0.1", "region").allowed


def test_unknown_class_is_unlimited() -> None:
    limiter = RateLimiter({"analysis": 1}, clock=FakeClock())
    for _ in range(100):
        assert limiter.check("10.0.0.1", "probes").allowed


def test_reset_clears_state() -> None:
    clock = FakeClock()
    limiter = RateLimiter({"analysis": 1}, clock=clock)
    assert limiter.check("10.0.0.1", "analysis").allowed
    assert not limiter.check("10.0.0.1", "analysis").allowed
    limiter.reset()
    assert limiter.check("10.0.0.1", "analysis").allowed


def test_classify_paths() -> None:
    assert classify("/healthz", "GET") is None
    assert classify("/readyz", "GET") is None
    assert classify("/docs", "GET") is None
    assert classify("/api/v1/admin/ingest-runs", "GET") == "admin"
    assert classify("/api/v1/admin/recompute", "POST") == "admin"
    assert classify("/api/v1/client-errors", "POST") == "client_errors"
    assert classify("/api/v1/regions/search", "GET") == "region"
    assert classify("/api/v1/regions", "POST") == "region"
    assert classify("/api/v1/regions", "GET") == "analysis"
    assert classify("/api/v1/analysis/timeseries", "GET") == "analysis"
    assert classify("/api/v1/hazards", "GET") == "analysis"
    # Trailing slash normalisation.
    assert classify("/api/v1/hazards/", "GET") == "analysis"


def test_retry_after_is_at_least_one_second() -> None:
    """A sub-second residual must still advertise a whole second of backoff."""
    clock = FakeClock()
    limiter = RateLimiter({"analysis": 60}, clock=clock)
    for _ in range(60):
        limiter.check("10.0.0.1", "analysis")
    clock.advance(0.5)  # half a token refilled
    decision = limiter.check("10.0.0.1", "analysis")
    assert not decision.allowed
    assert decision.retry_after_seconds == 1


def test_date_helpers_are_consistent() -> None:
    """V-02's default upper bound is last full year (DEC-04/DEC-17)."""
    assert last_full_year() == current_year() - 1
    assert date(current_year(), 1, 1).year == current_year()


def test_point_roundtrip() -> None:
    p = Point(lat=1.5, lon=-2.25)
    assert Point.model_validate(p.model_dump()) == p
