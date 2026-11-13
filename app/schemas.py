"""Activation request and response bodies: validation and MAC normalization.

A body that fails validation gets 422 in FastAPI's usual error shape (the
endpoint validates the raw bytes itself, see app/main.py).
"""

import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# AA:BB:CC:DD:EE:FF or aa-bb-cc-dd-ee-ff (any case, one separator used
# throughout), or 12 hex digits with no separator.
_MAC_RE = re.compile(
    r"[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}|[0-9A-Fa-f]{12}"
)


def normalize_mac(value: str) -> str:
    """Return the MAC in uppercase colon form (AA:BB:CC:DD:EE:FF), or raise ValueError.

    Runs during validation, so the unique key, the stored value and (from
    step 5) the per-device key all see only the normalized form. Without it,
    00-1a-2b-3c-4d-01 and 00:1A:2B:3C:4D:01 would be counted as two devices.
    """
    if not _MAC_RE.fullmatch(value):
        raise ValueError(
            "device_id must be a MAC address: AA:BB:CC:DD:EE:FF, "
            "aa-bb-cc-dd-ee-ff or 12 hex digits"
        )
    hex12 = value.replace(":", "").replace("-", "").upper()
    first_octet = int(hex12[:2], 16)

    # These addresses do not identify one physical device, and accepting
    # them would silently break the count: firmware that failed to read its
    # MAC sends all zeros, so many devices would be counted as one; a
    # randomized (locally administered) MAC can change between boots, so
    # one device would be counted several times. A 422 fails loudly, so
    # the device team sees it and fixes the firmware.
    if hex12 == "000000000000":
        raise ValueError("device_id is all zeros, not a device's MAC")
    if hex12 == "FFFFFFFFFFFF":
        raise ValueError("device_id is the broadcast address, not a device's MAC")
    if first_octet & 0x01:
        raise ValueError("device_id is a multicast address, not a device's MAC")
    if first_octet & 0x02:
        raise ValueError(
            "device_id is a locally administered address (for example a "
            "randomized MAC), not the factory MAC"
        )
    return ":".join(hex12[i : i + 2] for i in range(0, 12, 2))


def _no_nul(value: str) -> str:
    # Postgres TEXT cannot store the NUL character. Without this check such
    # a value would pass validation and then fail at INSERT as a 500.
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


def _naive_as_utc(value: datetime) -> datetime:
    # device_time is stored, never trusted. A value without an offset is
    # taken as UTC. An aware value is passed on as is; Postgres stores
    # TIMESTAMPTZ in UTC. (Converting here could overflow for a broken clock
    # near year 1 or 9999 and turn bad device data into a 500.)
    if value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


MacAddress = Annotated[str, AfterValidator(normalize_mac)]
ModelName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(_no_nul),
]
OptionalText = Annotated[str, StringConstraints(max_length=64), AfterValidator(_no_nul)]
DeviceTime = Annotated[datetime, AfterValidator(_naive_as_utc)]


# Shown in the OpenAPI docs (schema and "Try it out" body).
ACTIVATION_EXAMPLE = {
    "device_id": "00:1A:2B:3C:4D:5E",
    "model": "X100",
    "os_version": "14.1",
    "firmware_version": "1.2.3",
    "region": "IL",
    "device_time": "2026-11-13T09:30:00Z",
}


class ActivationRequest(BaseModel):
    """What a device sends on boot. Only device_id and model are required:
    rejecting a real activation over a missing optional field would lose a count."""

    model_config = ConfigDict(
        extra="forbid",  # unknown fields -> 422
        json_schema_extra={"examples": [ACTIVATION_EXAMPLE]},
    )

    device_id: MacAddress
    model: ModelName
    os_version: OptionalText | None = None
    firmware_version: OptionalText | None = None
    region: OptionalText | None = None
    device_time: DeviceTime | None = None


class ActivationResponse(BaseModel):
    status: Literal["activated", "already_activated"]
    first_activated_at: datetime


# GET /api/v1/stats. "activated" is the count of first activations;
# "duplicates" is the count of repeat messages from known devices.
class StatsRange(BaseModel):
    from_: date = Field(serialization_alias="from")  # "from" is a Python keyword
    to: date
    model: str | None  # the model filter, or null for all models


class DayStats(BaseModel):
    day: date
    activated: int
    duplicates: int


class ModelStats(BaseModel):
    model: str
    activated: int
    duplicates: int


class Totals(BaseModel):
    activated: int
    duplicates: int


class StatsResponse(BaseModel):
    range: StatsRange
    daily: list[DayStats]  # one entry per day of the range, zeros included
    totals_in_range: Totals
    activated_before_range: int  # strictly before range.from, same model filter
    by_model: list[ModelStats]  # for the range, ignores the model filter
    all_time: Totals  # whole table, no filters
