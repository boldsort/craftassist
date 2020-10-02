"""
Copyright (c) Facebook, Inc. and its affiliates.
"""

import copy
import os
import sys

from math import sin, cos, pi
from typing import cast, Sequence

BASE_AGENT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.append(BASE_AGENT_ROOT)

from base_agent.base_util import *
import rotation

TICKS_PER_MC_DAY = 24000

LOOK = Tuple[float, float]


class MCTime(Time):
    def __init__(self, get_world_time):
        super().__init__()
        self.get_world_time = get_world_time

    def get_world_hour(self):
        # returns a fraction of a day.  0 is sunrise, .5 is sunset, 1.0 is next day
        return self.get_world_time() / TICKS_PER_MC_DAY


def adjacent(p):
    """Return the positions adjacent to position p"""
    return (
        (p[0] + 1, p[1], p[2]),
        (p[0] - 1, p[1], p[2]),
        (p[0], p[1] + 1, p[2]),
        (p[0], p[1] - 1, p[2]),
        (p[0], p[1], p[2] + 1),
        (p[0], p[1], p[2] - 1),
    )


def build_safe_diag_adjacent(bounds):
    """ bounds is [mx, Mx, my, My, mz, Mz],
    if nothing satisfies, returns empty list """

    def a(p):
        """Return the adjacent positions to p including diagonal adjaceny, within the bounds"""
        mx = max(bounds[0], p[0] - 1)
        my = max(bounds[2], p[1] - 1)
        mz = max(bounds[4], p[2] - 1)
        Mx = min(bounds[1] - 1, p[0] + 1)
        My = min(bounds[3] - 1, p[1] + 1)
        Mz = min(bounds[5] - 1, p[2] + 1)
        return [
            (x, y, z)
            for x in range(mx, Mx + 1)
            for y in range(my, My + 1)
            for z in range(mz, Mz + 1)
            if (x, y, z) != p
        ]

    return a


def capped_line_of_sight(agent, player_struct, cap=20):
    """Return the block directly in the entity's line of sight, or a point in the distance"""
    xsect = agent.get_player_line_of_sight(player_struct)
    if xsect is not None and euclid_dist(pos_to_np(xsect), pos_to_np(player_struct.pos)) <= cap:
        return pos_to_np(xsect)

    # default to cap blocks in front of entity
    vec = rotation.look_vec(player_struct.look.yaw, player_struct.look.pitch)
    return cap * np.array(vec) + to_block_pos(pos_to_np(player_struct.pos))


def cluster_areas(areas):
    """Cluster a list of areas so that intersected ones are unioned

       areas: list of tuple ((x, y, z), radius), each defines a cube
       where (x, y, z) is the center and radius is half the side length
    """

    def expand_xyzs(pos, radius):
        xmin, ymin, zmin = pos[0] - radius, pos[1] - radius, pos[2] - radius
        xmax, ymax, zmax = pos[0] + radius, pos[1] + radius, pos[2] + radius
        return xmin, xmax, ymin, ymax, zmin, zmax

    def is_intersecting(area1, area2):
        x1_min, x1_max, y1_min, y1_max, z1_min, z1_max = expand_xyzs(area1[0], area1[1])
        x2_min, x2_max, y2_min, y2_max, z2_min, z2_max = expand_xyzs(area2[0], area2[1])
        return (
            (x1_min <= x2_max and x1_max >= x2_min)
            and (y1_min <= y2_max and y1_max >= y2_min)
            and (z1_min <= z2_max and z1_max >= z2_min)
        )

    def merge_area(area1, area2):
        x1_min, x1_max, y1_min, y1_max, z1_min, z1_max = expand_xyzs(area1[0], area1[1])
        x2_min, x2_max, y2_min, y2_max, z2_min, z2_max = expand_xyzs(area2[0], area2[1])

        x_min, y_min, z_min = min(x1_min, x2_min), min(y1_min, y2_min), min(z1_min, z2_min)
        x_max, y_max, z_max = max(x1_max, x2_max), max(y1_max, y2_max), max(z1_max, z2_max)

        x, y, z = (x_min + x_max) // 2, (y_min + y_max) // 2, (z_min + z_max) // 2
        radius = max(
            (x_max - x_min + 1) // 2, max((y_max - y_min + 1) // 2, (z_max - z_min + 1) // 2)
        )
        return ((x, y, z), radius)

    unclustered_areas = copy.deepcopy(areas)
    clustered_areas = []

    while len(unclustered_areas) > 0:
        area = unclustered_areas[0]
        del unclustered_areas[0]
        finished = True
        idx = 0
        while idx < len(unclustered_areas) or not finished:
            if idx >= len(unclustered_areas):
                idx = 0
                finished = True
                continue
            if is_intersecting(area, unclustered_areas[idx]):
                area = merge_area(area, unclustered_areas[idx])
                finished = False
                del unclustered_areas[idx]
            else:
                idx += 1
        clustered_areas.append(area)

    return clustered_areas


def diag_adjacent(p):
    """Return the adjacent positions to p including diagonal adjaceny"""
    return [
        (x, y, z)
        for x in range(p[0] - 1, p[0] + 2)
        for y in range(p[1] - 1, p[1] + 2)
        for z in range(p[2] - 1, p[2] + 2)
        if (x, y, z) != p
    ]


def discrete_step_dir(agent):
    """Discretized unit vector in the direction of agent's yaw

    agent pos + discrete_step_dir = block in front of agent
    """
    yaw = agent.get_player().look.yaw
    x = round(-sin(yaw * pi / 180))
    z = round(cos(yaw * pi / 180))
    return np.array([x, 0, z], dtype="int32")


def euclid_dist(a, b):
    """Return euclidean distance between a and b"""
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def fill_idmeta(agent, poss: List[XYZ]) -> List[Block]:
    """Add id_meta information to a a list of (xyz)s"""
    if len(poss) == 0:
        return []
    mx, my, mz = np.min(poss, axis=0)
    Mx, My, Mz = np.max(poss, axis=0)
    B = agent.get_blocks(mx, Mx, my, My, mz, Mz)
    idms = []
    for x, y, z in poss:
        idm = tuple(B[y - my, z - mz, x - mx])
        idms.append(cast(IDM, idm))
    return [(cast(XYZ, tuple(pos)), idm) for (pos, idm) in zip(poss, idms)]


def get_locs_from_entity(e):
    """Assumes input is either mob, memory, or tuple/list of coords
    outputs a tuple of coordinate tuples"""

    if hasattr(e, "pos"):
        if type(e.pos) is list or type(e.pos) is tuple or hasattr(e.pos, "dtype"):
            return (tuple(to_block_pos(e.pos)),)
        else:
            return tuple((tuple(to_block_pos(pos_to_np(e.pos))),))

    if str(type(e)).find("memory") > 0:
        if hasattr(e, "blocks"):
            return strip_idmeta(e.blocks)
        return None
    elif type(e) is tuple or type(e) is list:
        if len(e) > 0:
            if type(e[0]) is tuple:
                return e
            else:
                return tuple((e,))
    return None


# this should eventually be replaced with sql query
def most_common_idm(idms):
    """ idms is a list of tuples [(id, m) ,.... (id', m')]"""
    counts = {}
    for idm in idms:
        if not counts.get(idm):
            counts[idm] = 1
        else:
            counts[idm] += 1
    return max(counts, key=counts.get)


# TODO move this to "reasoning"
def object_looked_at(
    agent,
    candidates: Sequence[Tuple[XYZ, T]],
    player_struct,
    limit=1,
    max_distance=30,
    loose=False,
) -> List[Tuple[XYZ, T]]:
    """Return the object that `player` is looking at

    Args:
    - agent: agent object, for API access
    - candidates: list of (centroid, object) tuples
    - player_struct: player struct whose POV to use for calculation
    - limit: 'ALL' or int; max candidates to return
    - loose:  if True, don't filter candaidates behind agent

    Returns: a list of (xyz, mem) tuples, max length `limit`
    """
    if len(candidates) == 0:
        return []

    pos = pos_to_np(player_struct.pos)
    yaw, pitch = player_struct.look.yaw, player_struct.look.pitch

    # append to each candidate its relative position to player, rotated to
    # player-centric coordinates
    candidates_ = [(p, obj, rotation.transform(p - pos, yaw, pitch)) for (p, obj) in candidates]
    FRONT = rotation.DIRECTIONS["FRONT"]
    LEFT = rotation.DIRECTIONS["LEFT"]
    UP = rotation.DIRECTIONS["UP"]

    # reject objects behind player or not in cone of sight (but always include
    # an object if it's directly looked at)
    xsect = tuple(capped_line_of_sight(agent, player_struct, 25))
    if not loose:
        candidates_ = [
            (p, o, r)
            for (p, o, r) in candidates_
            if xsect in getattr(o, "blocks", {})
            or r @ FRONT > ((r @ LEFT) ** 2 + (r @ UP) ** 2) ** 0.5
        ]

    # if looking directly at an object, sort by proximity to look intersection
    if euclid_dist(pos, xsect) <= 25:
        candidates_.sort(key=lambda c: euclid_dist(c[0], xsect))
    else:
        # otherwise, sort by closest to look vector
        candidates_.sort(key=lambda c: ((c[2] @ LEFT) ** 2 + (c[2] @ UP) ** 2) ** 0.5)
    # linit returns of things too far away
    candidates_ = [c for c in candidates_ if euclid_dist(pos, c[0]) < max_distance]
    # limit number of returns
    if limit == "ALL":
        limit = len(candidates_)
    return [(p, o) for (p, o, r) in candidates_[:limit]]


def strip_idmeta(blockobj):
    """Return a list of (x, y, z) and drop the id_meta for blockobj"""
    if blockobj is not None:
        if type(blockobj) is dict:
            return list(pos for (pos, id_meta) in blockobj.items())
        else:
            return list(pos for (pos, id_meta) in blockobj)
    else:
        return None


def to_block_center(array):
    """Return the array centered at [0.5, 0.5, 0.5]"""
    return to_block_pos(array).astype("float") + [0.5, 0.5, 0.5]


def to_block_pos(array):
    """Convert array to block position"""
    return np.floor(array).astype("int32")
