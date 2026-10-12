"""TC-U-09 — unit assertion and conversion (docs/04 §2, docs/02 §5.6, EC-05)."""

from __future__ import annotations

import pytest

from app.providers.base import UnitMismatchError
from app.providers.units import normalize_temperature_unit, to_celsius


@pytest.mark.parametrize("unit", ["°C", "C", "celsius", " Celsius "])
def test_celsius_units_accepted(unit: str) -> None:
    assert normalize_temperature_unit(unit) == "celsius"


def test_fahrenheit_converts_exactly() -> None:
    assert normalize_temperature_unit("°F") == "fahrenheit"
    assert to_celsius(32.0, "°F") == pytest.approx(0.0)
    assert to_celsius(212.0, "°F") == pytest.approx(100.0)
    assert to_celsius(98.6, "°F") == pytest.approx(37.0, abs=0.01)


def test_celsius_passthrough() -> None:
    assert to_celsius(38.5, "°C") == 38.5


@pytest.mark.parametrize("unit", ["kelvin", "K", "°", "", "m/s"])
def test_unknown_unit_is_fatal_never_guessed(unit: str) -> None:
    with pytest.raises(UnitMismatchError) as excinfo:
        normalize_temperature_unit(unit)
    assert excinfo.value.code == "PROVIDER_UNIT_MISMATCH"
    assert excinfo.value.retryable is False


def test_missing_unit_is_fatal() -> None:
    with pytest.raises(UnitMismatchError):
        normalize_temperature_unit(None)
