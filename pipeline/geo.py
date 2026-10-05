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


# --- Helpers added for the PostGIS pipeline (clipping, containment, representative points) ----------------------
# Bounding boxes are (west, south, east, north) tuples; coordinates are (lon, lat) pairs.


def point_in_bbox(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> bool:
    """True when the point is inside or on the edge of the box."""
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north


def _clip_segment(
    x1: float, y1: float, x2: float, y2: float, bbox: tuple[float, float, float, float]
) -> tuple[float, float] | None:
    """Liang-Barsky: parameter range (t0, t1) of the part of the segment inside the box, or None."""
    west, south, east, north = bbox
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - west), (dx, east - x1), (-dy, y1 - south), (dy, north - y1)):
        if p == 0.0:
            if q < 0.0:
                return None  # parallel to this edge and outside it
            continue
        t = q / p
        if p < 0.0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (t0, t1)


def clip_line_to_bbox(
    coords: list[tuple[float, float]], bbox: tuple[float, float, float, float]
) -> list[list[tuple[float, float]]]:
    """Clip a polyline to a rectangle.

    Returns the parts of the line that lie inside the box (edge included), in order along the line; a line
    that leaves and re-enters the box yields several parts. Entry and exit vertices are placed exactly on the
    box edge. Parts with fewer than two distinct vertices are dropped.
    """
    west, south, east, north = bbox
    parts: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    def close_part() -> None:
        nonlocal current
        if len(current) >= 2:
            parts.append(current)
        current = []

    def at(x1: float, y1: float, x2: float, y2: float, t: float) -> tuple[float, float]:
        if t <= 0.0:
            return (x1, y1)
        if t >= 1.0:
            return (x2, y2)
        x = min(max(x1 + (x2 - x1) * t, west), east)
        y = min(max(y1 + (y2 - y1) * t, south), north)
        return (x, y)

    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        span = _clip_segment(x1, y1, x2, y2, bbox)
        if span is None:
            close_part()
            continue
        t0, t1 = span
        start, end = at(x1, y1, x2, y2, t0), at(x1, y1, x2, y2, t1)
        if t0 > 0.0 or not current:
            close_part()  # the line (re-)enters the box here
            current = [start]
        if end != current[-1]:
            current.append(end)
        if t1 < 1.0:
            close_part()  # the line leaves the box inside this segment
    close_part()
    return parts


def point_in_polygon(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test for one ring (closed or open; points on the edge are undefined)."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def ring_signed_area_deg(ring: list[tuple[float, float]]) -> float:
    """Signed area of a ring in square degrees (positive = counter-clockwise). Ring may be closed or open."""
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def polygon_area_centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """Area-weighted centroid of a ring (the planar centroid PostGIS ST_Centroid returns for the polygon).

    Falls back to the vertex average for degenerate (zero-area) rings.
    """
    pts = list(ring[:-1]) if len(ring) > 1 and tuple(ring[0]) == tuple(ring[-1]) else list(ring)
    if not pts:
        raise ValueError("empty ring")
    # Work relative to the first vertex to keep the products small (better floating-point conditioning).
    x0, y0 = pts[0][0], pts[0][1]
    area2 = cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i][0] - x0, pts[i][1] - y0
        bx, by = pts[(i + 1) % n][0] - x0, pts[(i + 1) % n][1] - y0
        cross = ax * by - bx * ay
        area2 += cross
        cx += (ax + bx) * cross
        cy += (ay + by) * cross
    if area2 == 0.0:
        return polygon_centroid(pts)
    return (x0 + cx / (3.0 * area2), y0 + cy / (3.0 * area2))


def point_along(coords: list[tuple[float, float]], fraction: float = 0.5) -> tuple[float, float]:
    """Point at the given fraction of the polyline's length (0.5 = the midpoint, which lies on the line)."""
    if not coords:
        raise ValueError("empty line")
    if len(coords) == 1:
        return (coords[0][0], coords[0][1])
    total = line_length(coords)
    if total == 0.0:
        return (coords[0][0], coords[0][1])
    target = min(max(fraction, 0.0), 1.0) * total
    walked = 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        seg = haversine(x1, y1, x2, y2)
        if seg > 0.0 and walked + seg >= target:
            t = (target - walked) / seg
            return (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        walked += seg
    return (coords[-1][0], coords[-1][1])


def point_to_line_m(lon: float, lat: float, coords: list[tuple[float, float]]) -> float:
    """Shortest distance in metres from a point to a polyline."""
    if len(coords) == 1:
        return haversine(lon, lat, coords[0][0], coords[0][1])
    return min(point_to_segment_m(lon, lat, x1, y1, x2, y2) for (x1, y1), (x2, y2) in zip(coords, coords[1:]))


# --- Helpers added for sensor placement (points on lines, small offsets, interior points) -----------------------


def metres_per_degree(lat: float) -> tuple[float, float]:
    """Metres per degree of longitude and of latitude at a latitude (the constants the helpers above use)."""
    return (111320.0 * math.cos(math.radians(lat)), 110540.0)


def offset_point(lon: float, lat: float, east_m: float, north_m: float) -> tuple[float, float]:
    """The point moved by a few metres east and north (planar approximation, fine at city scale)."""
    mx, my = metres_per_degree(lat)
    return (lon + east_m / mx, lat + north_m / my)


def point_at_distance(coords: list[tuple[float, float]], distance_m: float) -> tuple[float, float]:
    """Point at a distance in metres along a polyline (clamped to the line's ends)."""
    total = line_length(coords) if len(coords) > 1 else 0.0
    if total == 0.0:
        return (coords[0][0], coords[0][1])
    return point_along(coords, distance_m / total)


def interior_point(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """A point inside a simple polygon ring.

    The area centroid when it lies inside the ring (convex and most ordinary footprints); otherwise the
    middle of the widest horizontal chord at the centroid's latitude (L- and U-shaped footprints).
    """
    cx, cy = polygon_area_centroid(ring)
    if point_in_polygon(cx, cy, ring):
        return (cx, cy)
    pts = list(ring[:-1]) if len(ring) > 1 and tuple(ring[0]) == tuple(ring[-1]) else list(ring)
    lats = [p[1] for p in pts]
    for lat in (cy, (min(lats) + max(lats)) / 2.0):
        crossings: list[float] = []
        for i in range(len(pts)):
            (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
            if (y1 > lat) != (y2 > lat):
                crossings.append(x1 + (x2 - x1) * (lat - y1) / (y2 - y1))
        crossings.sort()
        chords = [(crossings[i + 1] - crossings[i], crossings[i]) for i in range(0, len(crossings) - 1, 2)]
        if chords:
            width, start = max(chords)
            return (start + width / 2.0, lat)
    return (cx, cy)
