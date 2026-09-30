"""Small, dependency-free geometry helpers (WGS84 in, metres out)."""
import math

EARTH_R = 6371008.8


def haversine(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def line_length(coords):
    return sum(haversine(*coords[i], *coords[i + 1]) for i in range(len(coords) - 1))


def interpolate_along(coords, spacing_m, offset_m=0.0):
    """Points every `spacing_m` metres along a lon/lat polyline."""
    pts, carry = [], spacing_m - offset_m
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        seg = haversine(x1, y1, x2, y2)
        if seg == 0:
            continue
        d = spacing_m - carry
        while d <= seg:
            t = d / seg
            pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
            d += spacing_m
        carry = seg - (d - spacing_m)
    return pts


def offset_line(coords, metres):
    """Parallel offset of a polyline (approximate, fine at city scale)."""
    if len(coords) < 2:
        return coords
    lat0 = coords[0][1]
    mx = 111320 * math.cos(math.radians(lat0))
    my = 110540
    out = []
    for i, (x, y) in enumerate(coords):
        a = coords[max(i - 1, 0)]
        b = coords[min(i + 1, len(coords) - 1)]
        dx, dy = (b[0] - a[0]) * mx, (b[1] - a[1]) * my
        n = math.hypot(dx, dy) or 1
        out.append((x + (-dy / n) * metres / mx, y + (dx / n) * metres / my))
    return out


def polygon_centroid(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def polygon_area_m2(ring):
    lat0 = ring[0][1]
    mx = 111320 * math.cos(math.radians(lat0))
    my = 110540
    a = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        a += (x1 * mx) * (y2 * my) - (x2 * mx) * (y1 * my)
    return abs(a) / 2


def square_polygon(lon, lat, half_m):
    """Small square footprint (used to render sensors as 3D columns)."""
    dx = half_m / (111320 * math.cos(math.radians(lat)))
    dy = half_m / 110540
    return [[lon - dx, lat - dy], [lon + dx, lat - dy], [lon + dx, lat + dy],
            [lon - dx, lat + dy], [lon - dx, lat - dy]]


def point_to_segment_m(px, py, ax, ay, bx, by):
    mx = 111320 * math.cos(math.radians(py))
    my = 110540
    px, py, ax, ay, bx, by = px * mx, py * my, ax * mx, ay * my, bx * mx, by * my
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0 if l2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def round_coords(coords, nd=6):
    if isinstance(coords[0], (int, float)):
        return [round(c, nd) for c in coords]
    return [round_coords(c, nd) for c in coords]
