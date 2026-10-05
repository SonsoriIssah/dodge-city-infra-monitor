"""Geometry helpers (``pipeline.geo``) and the line clipping rule of the processing stage. No database."""

from __future__ import annotations

import math

import pytest

from pipeline import geo
from pipeline.config import parse_bbox
from pipeline.gis.process import MIN_LINE_PART_M, clip_line

BOX = (-100.0, 37.0, -99.0, 38.0)  # west, south, east, north
M_PER_DEG_LAT = math.pi / 180.0 * 6371008.8  # one degree of a great circle on the mean-radius sphere


# --- distances ----------------------------------------------------------------------------------------------------
def test_haversine_of_one_degree_of_latitude():
    assert geo.haversine(-100.0, 37.0, -100.0, 38.0) == pytest.approx(M_PER_DEG_LAT, rel=1e-9)
    assert pytest.approx(111_195.08, abs=0.01) == M_PER_DEG_LAT


def test_haversine_of_longitude_shrinks_with_the_cosine_of_latitude():
    at_equator = geo.haversine(0.0, 0.0, 1.0, 0.0)
    at_sixty = geo.haversine(0.0, 60.0, 1.0, 60.0)
    assert at_equator == pytest.approx(M_PER_DEG_LAT, rel=1e-9)
    assert at_sixty == pytest.approx(at_equator / 2.0, rel=1e-3)


def test_haversine_is_symmetric_and_zero_for_the_same_point():
    a, b = (-100.0172, 37.7528), (-100.0101, 37.7590)
    assert geo.haversine(*a, *b) == pytest.approx(geo.haversine(*b, *a), rel=1e-12)
    assert geo.haversine(*a, *a) == 0.0


def test_line_length_is_the_sum_of_its_segments():
    line = [(-100.0, 37.0), (-100.0, 37.001), (-99.999, 37.001)]
    expected = geo.haversine(*line[0], *line[1]) + geo.haversine(*line[1], *line[2])
    assert geo.line_length(line) == pytest.approx(expected, rel=1e-12)
    assert geo.line_length(line[:1]) == 0


def test_point_along_returns_a_point_on_the_line_at_the_requested_share():
    line = [(-100.0, 37.0), (-100.0, 37.002), (-100.0, 37.004)]
    assert geo.point_along(line, 0.5) == pytest.approx((-100.0, 37.002))
    assert geo.point_along(line, 0.25) == pytest.approx((-100.0, 37.001))
    assert geo.point_along(line, 0.0) == line[0]
    assert geo.point_along(line, 7.0) == pytest.approx(line[-1])  # clamped


def test_point_at_distance_walks_metres_along_the_line():
    line = [(-100.0, 37.0), (-100.0, 37.01)]
    lon, lat = geo.point_at_distance(line, 100.0)
    assert geo.haversine(*line[0], lon, lat) == pytest.approx(100.0, abs=0.01)
    assert geo.point_at_distance(line, 1e9) == pytest.approx(line[-1])


def test_point_to_line_distance():
    line = [(-100.0, 37.0), (-100.0, 37.01)]
    east_m, _ = geo.metres_per_degree(37.005)
    assert geo.point_to_line_m(-100.0 + 50.0 / east_m, 37.005, line) == pytest.approx(50.0, abs=0.05)
    # beyond the end of the line the distance is measured to the end point
    assert geo.point_to_line_m(-100.0, 37.011, line) == pytest.approx(0.001 * 110540.0, rel=1e-6)


def test_offset_line_is_parallel_at_the_requested_distance():
    line = [(-100.0, 37.0), (-100.0, 37.001), (-100.0, 37.002)]
    shifted = geo.offset_line(line, 4.5)
    assert len(shifted) == len(line)
    for point in shifted:
        assert geo.point_to_line_m(point[0], point[1], line) == pytest.approx(4.5, abs=0.01)
    opposite = geo.offset_line(line, -4.5)
    assert (shifted[0][0] - line[0][0]) * (opposite[0][0] - line[0][0]) < 0  # the other side


def test_offset_point_moves_by_metres():
    lon, lat = geo.offset_point(-100.0, 37.75, 30.0, -40.0)
    assert geo.haversine(-100.0, 37.75, lon, lat) == pytest.approx(50.0, rel=5e-3)
    assert lon > -100.0 and lat < 37.75


# --- clipping -----------------------------------------------------------------------------------------------------
def test_a_line_inside_the_box_is_returned_unchanged():
    line = [(-99.8, 37.2), (-99.5, 37.5), (-99.2, 37.8)]
    assert geo.clip_line_to_bbox(line, BOX) == [line]


def test_a_line_outside_the_box_yields_nothing():
    assert geo.clip_line_to_bbox([(-101.0, 36.0), (-100.5, 36.5)], BOX) == []
    assert geo.clip_line_to_bbox([(-101.0, 37.5), (-100.5, 38.5)], BOX) == []  # passes the corner outside


def test_a_crossing_line_is_cut_exactly_on_the_edge():
    parts = geo.clip_line_to_bbox([(-99.5, 37.5), (-98.5, 37.5)], BOX)
    assert parts == [[(-99.5, 37.5), (-99.0, 37.5)]]
    diagonal = geo.clip_line_to_bbox([(-100.5, 36.5), (-99.5, 37.5)], BOX)
    assert diagonal == [[pytest.approx((-100.0, 37.0)), (-99.5, 37.5)]]


def test_a_line_through_the_box_is_cut_at_both_edges():
    parts = geo.clip_line_to_bbox([(-100.5, 37.25), (-98.5, 37.75)], BOX)
    assert len(parts) == 1
    (x1, y1), (x2, y2) = parts[0]
    assert (x1, x2) == (-100.0, -99.0)
    assert (y1, y2) == pytest.approx((37.375, 37.625))


def test_a_line_that_leaves_and_re_enters_gives_several_parts_in_order():
    line = [(-99.9, 37.5), (-99.9, 38.5), (-99.5, 38.5), (-99.5, 37.5), (-99.5, 36.5)]
    parts = geo.clip_line_to_bbox(line, BOX)
    assert parts == [[(-99.9, 37.5), (-99.9, 38.0)], [(-99.5, 38.0), (-99.5, 37.5), (-99.5, 37.0)]]


def test_a_vertex_on_the_edge_counts_as_inside():
    line = [(-100.0, 37.5), (-99.5, 37.5)]
    assert geo.clip_line_to_bbox(line, BOX) == [line]
    assert geo.point_in_bbox(-100.0, 38.0, BOX)
    assert not geo.point_in_bbox(-100.0000001, 37.5, BOX)


def test_clip_line_keeps_the_longest_of_several_parts_and_reports_the_rest():
    bbox = parse_bbox("37.0,-100.0,38.0,-99.0")
    line = [(-99.9, 37.9), (-99.9, 38.5), (-99.5, 38.5), (-99.5, 37.5), (-99.5, 36.5)]
    clipped = clip_line(line, bbox)
    assert clipped.coords == [[-99.5, 38.0], [-99.5, 37.5], [-99.5, 37.0]]  # 1 degree beats 0.1 degree
    assert clipped.parts == 2
    assert clipped.was_clipped is True
    assert clipped.length_m == pytest.approx(M_PER_DEG_LAT, rel=1e-6)
    assert clipped.dropped_m == pytest.approx(0.1 * M_PER_DEG_LAT, rel=1e-4)


def test_clip_line_length_is_the_clipped_length_not_the_original():
    bbox = parse_bbox("37.0,-100.0,38.0,-99.0")
    line = [(-99.5, 37.5), (-99.5, 38.5)]
    clipped = clip_line(line, bbox)
    assert clipped.length_m == pytest.approx(geo.line_length(line) / 2.0, rel=1e-6)
    assert clipped.was_clipped is True and clipped.parts == 1


def test_clip_line_reports_an_untouched_line_as_not_clipped():
    bbox = parse_bbox("37.0,-100.0,38.0,-99.0")
    clipped = clip_line([(-99.6, 37.5), (-99.5, 37.5)], bbox)
    assert clipped.was_clipped is False and clipped.dropped_m == 0.0


def test_clip_line_drops_parts_shorter_than_five_metres():
    bbox = parse_bbox("37.0,-100.0,38.0,-99.0")
    _, north_m = geo.metres_per_degree(37.999)
    assert MIN_LINE_PART_M == 5.0
    short = [(-99.5, 38.0 - 4.0 / north_m), (-99.5, 38.5)]  # 4 m inside, the rest outside
    assert clip_line(short, bbox) is None
    enough = [(-99.5, 38.0 - 6.0 / north_m), (-99.5, 38.5)]  # 6 m inside
    assert clip_line(enough, bbox).length_m == pytest.approx(6.0, abs=0.11)


def test_clip_line_never_returns_a_vertex_outside_the_box():
    bbox = parse_bbox("37.745,-100.030,37.762,-100.005")
    line = [(-100.0312345, 37.7501234), (-100.0123456, 37.7634567), (-100.0012345, 37.7445678)]
    clipped = clip_line(line, bbox)
    assert clipped is not None
    assert all(bbox.contains(lon, lat) for lon, lat in clipped.coords)


# --- polygons -----------------------------------------------------------------------------------------------------
SQUARE = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
L_SHAPE = [(0.0, 0.0), (3.0, 0.0), (3.0, 1.0), (1.0, 1.0), (1.0, 3.0), (0.0, 3.0)]
U_SHAPE = [(0.0, 0.0), (3.0, 0.0), (3.0, 3.0), (2.0, 3.0), (2.0, 1.0), (1.0, 1.0), (1.0, 3.0), (0.0, 3.0)]


@pytest.mark.parametrize(
    ("point", "ring", "inside"),
    [
        ((1.0, 1.0), SQUARE, True),
        ((2.5, 1.0), SQUARE, False),
        ((-0.1, 1.0), SQUARE, False),
        ((0.5, 2.5), L_SHAPE, True),
        ((2.0, 2.0), L_SHAPE, False),  # in the notch of the L
        ((2.5, 0.5), L_SHAPE, True),
        ((1.5, 2.0), U_SHAPE, False),  # between the arms of the U
        ((1.5, 0.5), U_SHAPE, True),
    ],
)
def test_point_in_polygon(point, ring, inside):
    assert geo.point_in_polygon(point[0], point[1], ring) is inside
    assert geo.point_in_polygon(point[0], point[1], [*ring, ring[0]]) is inside  # closed ring, same answer


def test_point_in_polygon_needs_three_vertices():
    assert geo.point_in_polygon(0.5, 0.5, [(0.0, 0.0), (1.0, 1.0)]) is False


def test_polygon_area_of_a_hundred_metre_square():
    east_m, north_m = geo.metres_per_degree(37.75)
    lon0, lat0 = -100.02, 37.75
    ring = [
        (lon0, lat0),
        (lon0 + 100.0 / east_m, lat0),
        (lon0 + 100.0 / east_m, lat0 + 100.0 / north_m),
        (lon0, lat0 + 100.0 / north_m),
    ]
    assert geo.polygon_area_m2(ring) == pytest.approx(10_000.0, rel=1e-6)
    # orientation does not matter (the east-west scale is taken at the first vertex, hence the tolerance)
    assert geo.polygon_area_m2(ring[::-1]) == pytest.approx(10_000.0, rel=1e-4)


def test_signed_area_tells_the_orientation():
    assert geo.ring_signed_area_deg(SQUARE) == pytest.approx(4.0)
    assert geo.ring_signed_area_deg(SQUARE[::-1]) == pytest.approx(-4.0)
    assert geo.ring_signed_area_deg(L_SHAPE) == pytest.approx(5.0)


def test_area_centroid_is_area_weighted_not_the_vertex_average():
    assert geo.polygon_area_centroid(SQUARE) == pytest.approx((1.0, 1.0))
    # L shape = 3x1 bar (area 3, centre 1.5, 0.5) + 1x2 bar (area 2, centre 0.5, 2.0)
    assert geo.polygon_area_centroid(L_SHAPE) == pytest.approx((1.1, 1.1))
    assert geo.polygon_centroid(L_SHAPE) != pytest.approx((1.1, 1.1))
    assert geo.polygon_area_centroid([*L_SHAPE, L_SHAPE[0]]) == pytest.approx((1.1, 1.1))


def test_area_centroid_of_a_degenerate_ring_falls_back_to_the_vertex_average():
    assert geo.polygon_area_centroid([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)]) == pytest.approx((1.0, 1.0))
    with pytest.raises(ValueError):
        geo.polygon_area_centroid([])


@pytest.mark.parametrize("ring", [SQUARE, L_SHAPE, U_SHAPE], ids=["square", "L", "U"])
def test_interior_point_lies_inside_the_polygon(ring):
    lon, lat = geo.interior_point(ring)
    assert geo.point_in_polygon(lon, lat, ring)


def test_interior_point_of_a_u_shape_is_not_its_centroid():
    centroid = geo.polygon_area_centroid(U_SHAPE)
    assert not geo.point_in_polygon(centroid[0], centroid[1], U_SHAPE)  # the centroid falls between the arms
    assert geo.interior_point(U_SHAPE) != pytest.approx(centroid)


def test_round_coords_rounds_nested_coordinate_lists():
    assert geo.round_coords([[1.123456789, 2.987654321], [3.0, 4.0]], 3) == [[1.123, 2.988], [3.0, 4.0]]
    assert geo.round_coords([1.123456789, 2.987654321]) == [1.123457, 2.987654]
