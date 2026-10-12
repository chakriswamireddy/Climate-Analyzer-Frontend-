"""Unit normalization for provider payloads (docs/02 §5.6, TC-U-09, EC-05).

Open-Meteo reports °C; the adapter still reads ``daily_units`` and asserts it
rather than assuming, so a future NOAA (°F) adapter reuses this code and an
unknown unit fails the run instead of silently misreading Fahrenheit as
Celsius.
"""

from __future__ import annotations

from app.providers.base import UnitMismatchError

_CELSIUS = {"°C", "°c", "C", "c", "celsius", "Celsius"}
_FAHRENHEIT = {"°F", "°f", "F", "f", "fahrenheit", "Fahrenheit"}


def normalize_temperature_unit(unit: str | None) -> str:
    """Validate a declared unit; raise :class:`UnitMismatchError` if unknown."""
    if unit is None:
        raise UnitMismatchError("missing temperature unit in payload")
    cleaned = unit.strip()
    if cleaned in _CELSIUS:
        return "celsius"
    if cleaned in _FAHRENHEIT:
        return "fahrenheit"
    raise UnitMismatchError(f"unknown temperature unit: {unit!r}")


def to_celsius(value: float, unit: str) -> float:
    """Convert a temperature to °C after :func:`normalize_temperature_unit`."""
    kind = normalize_temperature_unit(unit)
    if kind == "fahrenheit":
        return round((value - 32.0) * 5.0 / 9.0, 2)
    return value
