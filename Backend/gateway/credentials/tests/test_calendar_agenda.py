from datetime import datetime, timezone

from gateway.credentials.routes import _to_google_rfc3339_z


def test_to_google_rfc3339_z_uses_single_utc_suffix():
    value = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)

    assert _to_google_rfc3339_z(value) == "2026-05-01T00:00:00Z"
    assert "+00:00Z" not in _to_google_rfc3339_z(value)
