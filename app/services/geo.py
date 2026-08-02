import math
import random


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points in kilometres."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def random_points_in_radius(
    center_lat: float,
    center_lng: float,
    radius_km: float,
    count: int,
    seed: int | None = None,
) -> list[tuple[float, float]]:
    """Uniform random points inside a circle (km) around a center."""
    rng = random.Random(seed)
    points: list[tuple[float, float]] = []
    for _ in range(count):
        # sqrt for uniform area distribution
        r = radius_km * math.sqrt(rng.random())
        theta = rng.random() * 2 * math.pi
        north_km = r * math.cos(theta)
        east_km = r * math.sin(theta)
        dlat = north_km / 111.32
        cos_lat = math.cos(math.radians(center_lat))
        dlng = east_km / (111.32 * cos_lat) if abs(cos_lat) > 1e-6 else 0.0
        points.append((center_lat + dlat, center_lng + dlng))
    return points


def within_radius(
    center_lat: float,
    center_lng: float,
    lat: float,
    lng: float,
    radius_km: float,
    tolerance_km: float = 0.05,
) -> bool:
    return haversine_km(center_lat, center_lng, lat, lng) <= radius_km + tolerance_km
