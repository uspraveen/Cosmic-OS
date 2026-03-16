from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_MONTH_ALIASES = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_DAY_ALIASES = {
    "SUN": 0,
    "MON": 1,
    "TUE": 2,
    "WED": 3,
    "THU": 4,
    "FRI": 5,
    "SAT": 6,
}

_UTC = timezone.utc
_MAX_SEARCH_MINUTES = 60 * 24 * 366 * 5


class CronExpressionError(ValueError):
    """Raised when a 5-field cron expression cannot be parsed safely."""


@dataclass(frozen=True, slots=True)
class _ParsedField:
    values: frozenset[int] | None

    @property
    def is_wildcard(self) -> bool:
        return self.values is None

    def matches(self, value: int) -> bool:
        return self.values is None or value in self.values


@dataclass(frozen=True, slots=True)
class ParsedCronExpression:
    raw: str
    minute: _ParsedField
    hour: _ParsedField
    day_of_month: _ParsedField
    month: _ParsedField
    day_of_week: _ParsedField

    def matches(self, local_dt: datetime) -> bool:
        if not self.minute.matches(local_dt.minute):
            return False
        if not self.hour.matches(local_dt.hour):
            return False
        if not self.month.matches(local_dt.month):
            return False

        day_of_month_match = self.day_of_month.matches(local_dt.day)
        cron_weekday = (local_dt.weekday() + 1) % 7
        day_of_week_match = self.day_of_week.matches(cron_weekday)

        if self.day_of_month.is_wildcard and self.day_of_week.is_wildcard:
            day_match = True
        elif self.day_of_month.is_wildcard:
            day_match = day_of_week_match
        elif self.day_of_week.is_wildcard:
            day_match = day_of_month_match
        else:
            day_match = day_of_month_match or day_of_week_match

        return day_match


def normalize_timezone_name(timezone_name: str) -> str:
    normalized = str(timezone_name or "").strip()
    if not normalized:
        raise CronExpressionError("timezone is required")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise CronExpressionError(f"Unknown timezone: {normalized}") from exc
    return normalized


def parse_cron_expression(expression: str) -> ParsedCronExpression:
    raw = str(expression or "").strip()
    parts = raw.split()
    if len(parts) != 5:
        raise CronExpressionError("Cron expression must contain exactly 5 fields.")

    return ParsedCronExpression(
        raw=raw,
        minute=_parse_field(parts[0], minimum=0, maximum=59, field_name="minute"),
        hour=_parse_field(parts[1], minimum=0, maximum=23, field_name="hour"),
        day_of_month=_parse_field(parts[2], minimum=1, maximum=31, field_name="day_of_month"),
        month=_parse_field(parts[3], minimum=1, maximum=12, field_name="month", aliases=_MONTH_ALIASES),
        day_of_week=_parse_field(parts[4], minimum=0, maximum=7, field_name="day_of_week", aliases=_DAY_ALIASES, wrap_sunday=True),
    )


def compute_next_fire_at(
    expression: str,
    timezone_name: str,
    *,
    after: datetime | None = None,
) -> str:
    parsed = parse_cron_expression(expression)
    resolved_timezone = ZoneInfo(normalize_timezone_name(timezone_name))
    current = after or datetime.now(_UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_UTC)
    else:
        current = current.astimezone(_UTC)

    candidate = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_MAX_SEARCH_MINUTES):
        local_candidate = candidate.astimezone(resolved_timezone)
        if parsed.matches(local_candidate):
            return candidate.isoformat().replace("+00:00", "Z")
        candidate += timedelta(minutes=1)

    raise CronExpressionError("Cron expression did not produce a fire time within the search window.")


def render_local_fire_time(when_iso: str | None, timezone_name: str) -> str | None:
    if not when_iso:
        return None
    current = _parse_iso_timestamp(when_iso)
    if current is None:
        return None
    try:
        local_dt = current.astimezone(ZoneInfo(normalize_timezone_name(timezone_name)))
    except CronExpressionError:
        return None
    return local_dt.strftime("%A, %B %d, %Y at %I:%M %p %Z")


def _parse_field(
    raw_field: str,
    *,
    minimum: int,
    maximum: int,
    field_name: str,
    aliases: dict[str, int] | None = None,
    wrap_sunday: bool = False,
) -> _ParsedField:
    token = raw_field.strip().upper()
    if not token:
        raise CronExpressionError(f"{field_name} field cannot be empty.")
    if token == "*":
        return _ParsedField(values=None)

    values: set[int] = set()
    for fragment in token.split(","):
        fragment = fragment.strip()
        if not fragment:
            raise CronExpressionError(f"{field_name} field contains an empty fragment.")
        step = 1
        range_part = fragment
        if "/" in fragment:
            range_part, step_part = fragment.split("/", 1)
            step = _coerce_number(step_part, aliases=aliases, field_name=field_name)
            if step <= 0:
                raise CronExpressionError(f"{field_name} field has a non-positive step.")
        start: int
        end: int
        if range_part == "*":
            start = minimum
            end = maximum
        elif "-" in range_part:
            start_part, end_part = range_part.split("-", 1)
            start = _coerce_number(start_part, aliases=aliases, field_name=field_name)
            end = _coerce_number(end_part, aliases=aliases, field_name=field_name)
        else:
            start = _coerce_number(range_part, aliases=aliases, field_name=field_name)
            end = start
        if wrap_sunday:
            if start == 7:
                start = 0
            if end == 7:
                end = 0
        if start < minimum or start > maximum or end < minimum or end > maximum:
            raise CronExpressionError(f"{field_name} field value is out of range.")
        if start <= end:
            sequence = range(start, end + 1, step)
        else:
            if not wrap_sunday:
                raise CronExpressionError(f"{field_name} field range is invalid.")
            sequence = list(range(start, maximum + 1, step)) + list(range(minimum, end + 1, step))
        for value in sequence:
            values.add(0 if wrap_sunday and value == 7 else value)
    if not values:
        raise CronExpressionError(f"{field_name} field did not resolve to any values.")
    return _ParsedField(values=frozenset(sorted(values)))


def _coerce_number(raw_value: str, *, aliases: dict[str, int] | None, field_name: str) -> int:
    token = raw_value.strip().upper()
    if not token:
        raise CronExpressionError(f"{field_name} field contains an empty token.")
    if aliases and token in aliases:
        return aliases[token]
    try:
        return int(token)
    except ValueError as exc:
        raise CronExpressionError(f"{field_name} field contains an invalid token: {raw_value!r}") from exc


def _parse_iso_timestamp(raw_value: str) -> datetime | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)
