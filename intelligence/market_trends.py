"""Observed-data market digest (Phase 3).

Code aggregation over stored opportunities. No LLM. No invented taxonomy,
donors, or posting dates. See docs/adr/007-observed-market-digest.md.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from intelligence.organizations import (
    MatchResult,
    OrgIndexEntry,
    match_organization,
)

# Minimum dated stored rows in a window before any "trend" claim.
# n=2 (and anything below this) is INSUFFICIENT DATA, never INFERRED-as-trend.
TREND_MIN_N = 10
WINDOW_DAYS = (30, 90)
INSUFFICIENT_TREND_PHRASE = "insufficient data for a trend"

STATUS_VERIFIED = "VERIFIED"
STATUS_INSUFFICIENT = "INSUFFICIENT DATA"

PLACEHOLDER_LABELS = frozenset({
    "",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "tbd",
    "not specified",
    "not stated",
    "not provided",
    "-",
    "--",
})

_MAX_LABEL_CHARS = 200
_MAX_ROWS = 2000


@dataclass(frozen=True)
class ObservedOpportunity:
    """One stored opportunity row. Dates are parsed stored timestamps, or None."""

    source_id: str
    source: str  # supabase | airtable
    source_url: str
    title: str
    discovered_on: date | None
    themes: tuple[str, ...]
    locations: tuple[str, ...]
    donor: str
    malformed_theme_skips: int = 0
    malformed_location_skips: int = 0


@dataclass(frozen=True)
class LabelCount:
    label: str
    count: int


@dataclass(frozen=True)
class FrequencyWindow:
    days: int
    start: date
    end: date
    sample_size: int
    undated_skipped: int
    malformed_skipped: int
    counts: tuple[LabelCount, ...]
    is_trend: bool
    status: str
    message: str

    def sample_clause(self) -> str:
        return (
            f"n={self.sample_size} dated rows, "
            f"{self.start.isoformat()} to {self.end.isoformat()}"
        )


@dataclass(frozen=True)
class GeoShiftRow:
    label: str
    share_30: float
    share_90: float
    delta_pp: float
    count_30: int
    count_90: int


@dataclass(frozen=True)
class GeoShift:
    status: str
    message: str
    rows: tuple[GeoShiftRow, ...] = ()


@dataclass(frozen=True)
class DonorCount:
    organization_id: str
    canonical_name: str
    count: int
    match_status: str


@dataclass(frozen=True)
class DonorWindow:
    days: int
    start: date
    end: date
    sample_size: int
    is_trend: bool
    status: str
    message: str
    donors: tuple[DonorCount, ...]


@dataclass
class MarketDigest:
    as_of: date
    store_row_count: int
    dated_row_count: int
    truncated: bool
    sources: tuple[str, ...]
    themes: dict[int, FrequencyWindow] = field(default_factory=dict)
    geography: dict[int, FrequencyWindow] = field(default_factory=dict)
    geo_shift: GeoShift | None = None
    donor_orgs_available: bool = False
    donor_org_count: int = 0
    donor_windows: dict[int, DonorWindow] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def overall_is_trend(self) -> bool:
        w30 = self.themes.get(30)
        return bool(w30 and w30.is_trend)


def window_bounds(as_of: date, days: int) -> tuple[date, date]:
    """Inclusive trailing window: [as_of - (days-1), as_of]."""
    if days < 1:
        raise ValueError("window days must be >= 1")
    return as_of - timedelta(days=days - 1), as_of


def parse_observed_date(value) -> date | None:
    """Parse a stored discovery/created timestamp. Never invent a date."""
    if value is None or value is False:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float, bool, dict, list, tuple)):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z") and "T" in text:
        text = text[:-1] + "+00:00"
    try:
        if "T" in text:
            return datetime.fromisoformat(text).date()
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


def _usable_label(raw) -> str | None:
    if raw is None or isinstance(raw, (bool, int, float, dict, list, tuple)):
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    if len(text) > _MAX_LABEL_CHARS:
        text = text[:_MAX_LABEL_CHARS]
    if text.casefold() in PLACEHOLDER_LABELS:
        return None
    return text


def extract_labels(value) -> tuple[tuple[str, ...], int]:
    """Return observed string labels and a count of malformed items skipped.

    A missing/empty field is not malformed. Dicts, numbers, and nested
    structures are malformed and are not counted as themes/locations.
    A plain string is one label (not split into invented buckets).
    """
    if value is None:
        return (), 0
    if isinstance(value, str):
        label = _usable_label(value)
        return ((label,) if label else ()), 0
    if isinstance(value, (list, tuple)):
        labels: list[str] = []
        malformed = 0
        for item in value:
            if isinstance(item, (dict, list, tuple)) or isinstance(item, (int, float, bool)):
                malformed += 1
                continue
            if item is None:
                malformed += 1
                continue
            label = _usable_label(item)
            if label is None:
                # empty / placeholder: skip, not a theme, not a made-up bucket
                continue
            labels.append(label)
        return tuple(labels), malformed
    return (), 1


def _in_window(discovered_on: date | None, start: date, end: date) -> bool:
    if discovered_on is None:
        return False
    return start <= discovered_on <= end


def _display_and_key(label: str) -> tuple[str, str]:
    return label, label.casefold()


def _frequency_window(
    records: Sequence[ObservedOpportunity],
    *,
    as_of: date,
    days: int,
    field: str,
) -> FrequencyWindow:
    start, end = window_bounds(as_of, days)
    dated = 0
    undated = 0
    malformed = 0
    key_counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for row in records:
        if row.discovered_on is None:
            undated += 1
            continue
        if not _in_window(row.discovered_on, start, end):
            continue
        dated += 1
        if field == "themes":
            malformed += row.malformed_theme_skips
            labels = row.themes
        else:
            malformed += row.malformed_location_skips
            labels = row.locations
        seen_keys: set[str] = set()
        for label in labels:
            shown, key = _display_and_key(label)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            key_counts[key] += 1
            display.setdefault(key, shown)

    is_trend = dated >= TREND_MIN_N
    if is_trend:
        counts = tuple(
            LabelCount(label=display[key], count=key_counts[key])
            for key, _ in key_counts.most_common()
        )
        status = STATUS_VERIFIED
        message = (
            f"VERIFIED ({dated} stored rows, "
            f"{start.isoformat()} to {end.isoformat()})"
        )
    else:
        counts = ()
        status = STATUS_INSUFFICIENT
        message = (
            f"{INSUFFICIENT_TREND_PHRASE} "
            f"(n={dated} < {TREND_MIN_N}, {start.isoformat()} to {end.isoformat()})"
        )
    return FrequencyWindow(
        days=days,
        start=start,
        end=end,
        sample_size=dated,
        undated_skipped=undated,
        malformed_skipped=malformed,
        counts=counts,
        is_trend=is_trend,
        status=status,
        message=message,
    )


def _geo_shift(
    win_30: FrequencyWindow,
    win_90: FrequencyWindow,
) -> GeoShift:
    if not (win_30.is_trend and win_90.is_trend):
        return GeoShift(
            status=STATUS_INSUFFICIENT,
            message=(
                f"{INSUFFICIENT_TREND_PHRASE} for a geography shift "
                f"(30d n={win_30.sample_size}, 90d n={win_90.sample_size}; "
                f"need n>={TREND_MIN_N} in both windows)"
            ),
        )
    c30 = {row.label.casefold(): row for row in win_30.counts}
    c90 = {row.label.casefold(): row for row in win_90.counts}
    keys = set(c30) | set(c90)
    rows: list[GeoShiftRow] = []
    n30 = win_30.sample_size
    n90 = win_90.sample_size
    for key in keys:
        a = c30.get(key)
        b = c90.get(key)
        count_30 = a.count if a else 0
        count_90 = b.count if b else 0
        label = (a.label if a else None) or (b.label if b else key)
        share_30 = count_30 / n30
        share_90 = count_90 / n90
        rows.append(
            GeoShiftRow(
                label=label,
                share_30=share_30,
                share_90=share_90,
                delta_pp=(share_30 - share_90) * 100.0,
                count_30=count_30,
                count_90=count_90,
            )
        )
    rows.sort(key=lambda r: abs(r.delta_pp), reverse=True)
    return GeoShift(
        status=STATUS_VERIFIED,
        message=(
            f"VERIFIED geography share shift using stored rows "
            f"(30d n={n30}, {win_30.start.isoformat()} to {win_30.end.isoformat()}; "
            f"90d n={n90}, {win_90.start.isoformat()} to {win_90.end.isoformat()})"
        ),
        rows=tuple(rows),
    )


def _match_stored_donor(
    donor: str,
    index: Sequence[OrgIndexEntry],
) -> MatchResult | None:
    if not index:
        return None
    hit = match_organization(donor, index)
    if hit.method in {"empty", "new_candidate"} or not hit.organization_id:
        return None
    if hit.is_new_candidate:
        return None
    return hit


def _donor_window(
    records: Sequence[ObservedOpportunity],
    index: Sequence[OrgIndexEntry],
    *,
    as_of: date,
    days: int,
    orgs_available: bool,
    org_count: int,
) -> DonorWindow:
    start, end = window_bounds(as_of, days)
    dated = sum(
        1
        for row in records
        if row.discovered_on is not None and _in_window(row.discovered_on, start, end)
    )
    if not orgs_available or org_count == 0:
        return DonorWindow(
            days=days,
            start=start,
            end=end,
            sample_size=dated,
            is_trend=False,
            status=STATUS_INSUFFICIENT,
            message=(
                "insufficient data — organizations table missing or empty; "
                "donors were not invented"
            ),
            donors=(),
        )
    is_trend = dated >= TREND_MIN_N
    if not is_trend:
        return DonorWindow(
            days=days,
            start=start,
            end=end,
            sample_size=dated,
            is_trend=False,
            status=STATUS_INSUFFICIENT,
            message=(
                f"{INSUFFICIENT_TREND_PHRASE} for donor cadence "
                f"(n={dated} < {TREND_MIN_N}, {start.isoformat()} to {end.isoformat()})"
            ),
            donors=(),
        )

    counts: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    statuses: dict[str, str] = {}
    for row in records:
        if not _in_window(row.discovered_on, start, end):
            continue
        hit = _match_stored_donor(row.donor, index)
        if hit is None:
            continue
        counts[hit.organization_id] += 1
        names[hit.organization_id] = hit.canonical_name
        statuses[hit.organization_id] = hit.status

    donors = tuple(
        DonorCount(
            organization_id=oid,
            canonical_name=names[oid],
            count=counts[oid],
            match_status=statuses[oid],
        )
        for oid, _ in sorted(counts.items(), key=lambda kv: (-kv[1], names[kv[0]]))
    )
    if not donors:
        message = (
            f"INSUFFICIENT DATA for donor cadence — no stored opportunity donors "
            f"matched organizations already in the table "
            f"(n={dated} dated rows, {start.isoformat()} to {end.isoformat()}; "
            f"{org_count} organizations in index)"
        )
        status = STATUS_INSUFFICIENT
    else:
        message = (
            f"VERIFIED posting counts for donors already in the organizations "
            f"table ({dated} dated rows, {start.isoformat()} to {end.isoformat()})"
        )
        status = STATUS_VERIFIED
    return DonorWindow(
        days=days,
        start=start,
        end=end,
        sample_size=dated,
        is_trend=is_trend,
        status=status,
        message=message,
        donors=donors,
    )


def build_market_digest(
    records: Iterable[ObservedOpportunity] | None,
    *,
    as_of: date | None = None,
    org_index: Sequence[OrgIndexEntry] | None = None,
    orgs_available: bool = False,
    truncated: bool = False,
) -> MarketDigest:
    """Aggregate stored rows. Empty/thin input → honest insufficient digest."""
    as_of = as_of or date.today()
    rows = list(records or ())
    sources = tuple(sorted({row.source for row in rows if row.source}))
    dated = sum(1 for row in rows if row.discovered_on is not None)
    notes: list[str] = []
    if truncated:
        notes.append(
            f"Store listing truncated at {_MAX_ROWS} rows; remaining rows were not counted."
        )
    if not rows:
        notes.append(
            "No stored opportunities were found. INSUFFICIENT DATA. "
            "This digest does not invent market categories."
        )
    themes = {
        days: _frequency_window(rows, as_of=as_of, days=days, field="themes")
        for days in WINDOW_DAYS
    }
    geography = {
        days: _frequency_window(rows, as_of=as_of, days=days, field="locations")
        for days in WINDOW_DAYS
    }
    org_index = list(org_index or ())
    org_ids = {entry.organization_id for entry in org_index if entry.organization_id}
    org_count = len(org_ids)
    donor_windows = {
        days: _donor_window(
            rows,
            org_index,
            as_of=as_of,
            days=days,
            orgs_available=orgs_available,
            org_count=org_count,
        )
        for days in WINDOW_DAYS
    }
    return MarketDigest(
        as_of=as_of,
        store_row_count=len(rows),
        dated_row_count=dated,
        truncated=truncated,
        sources=sources,
        themes=themes,
        geography=geography,
        geo_shift=_geo_shift(geography[30], geography[90]),
        donor_orgs_available=orgs_available,
        donor_org_count=org_count,
        donor_windows=donor_windows,
        notes=tuple(notes),
    )


def max_store_rows() -> int:
    return _MAX_ROWS
