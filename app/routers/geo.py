"""Geocoding helpers — Nominatim proxied server-side to avoid browser CORS issues."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/geo", tags=["geo"])

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "ExperientiaCampaignOps/1.0 (campaign-location)"


def _nominatim_search(params: dict) -> list[dict]:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{NOMINATIM}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    return data if isinstance(data, list) else []


@router.get("/pincode/{pincode}")
def lookup_pincode(pincode: str):
    pin = pincode.strip()
    if not pin.isdigit() or len(pin) != 6:
        raise HTTPException(status_code=400, detail="Enter a valid 6-digit Indian pincode")

    attempts = [
        {
            "postalcode": pin,
            "country": "India",
            "countrycodes": "in",
            "format": "json",
            "limit": "1",
            "addressdetails": "1",
        },
        {
            "q": f"{pin}, India",
            "countrycodes": "in",
            "format": "json",
            "limit": "1",
            "addressdetails": "1",
        },
    ]

    place = None
    try:
        for params in attempts:
            results = _nominatim_search(params)
            if results:
                place = results[0]
                break
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail=f"Geocoding service unavailable: {exc}") from exc

    if not place:
        raise HTTPException(status_code=404, detail=f"No map location found for pincode {pin}")

    try:
        lat = float(place["lat"])
        lng = float(place["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Invalid geocode response") from exc

    address = place.get("address") or {}
    parts = [
        address.get("suburb") or address.get("neighbourhood") or address.get("city_district"),
        address.get("city") or address.get("town") or address.get("village") or address.get("state_district"),
        address.get("state"),
        pin,
    ]
    label = ", ".join(p for p in parts if p) or place.get("display_name") or f"Pincode {pin}"

    return {
        "lat": lat,
        "lng": lng,
        "label": label,
        "pincode": pin,
        "display_name": place.get("display_name"),
    }
