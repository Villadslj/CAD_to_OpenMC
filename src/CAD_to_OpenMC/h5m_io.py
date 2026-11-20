"""Reading and writing of DAGMC h5m-files with h5py.

The h5m-format is the HDF5 layout written by MOAB. Only the parts that make up
a DAGMC geometry are handled here: the vertices, the triangles, the entity sets
for surfaces, volumes and groups, and the tags that connect them. No MOAB
installation is needed.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import h5py
import numpy as np

from .meshed_model import MeshedModel, NO_VOLUME

MATERIAL_PREFIX = "mat:"
COMPLEMENT_SUFFIX = "_comp"
#: bit in the flags of a set that marks its contents as (first id, count) pairs
SET_RANGE_BIT = 0x8
#: flags of a set without and with range compressed contents
_SET, _SET_RANGES = 2, 2 | SET_RANGE_BIT
#: size in bytes of the NAME and CATEGORY tags of MOAB
_NAME_SIZE = 32
_ELEMENT_TYPES = {
    "Edge": 1, "Tri": 2, "Quad": 3, "Polygon": 4, "Tet": 5,
    "Pyramid": 6, "Prism": 7, "Knife": 8, "Hex": 9, "Polyhedron": 10,
}


def _opaque_to_str(value) -> str:
    return bytes(value).split(b"\0", 1)[0].decode("ascii", "replace")


def _expand_ranges(pairs) -> np.ndarray:
    """Turn (first id, count) pairs into the list of ids they stand for."""
    pairs = np.asarray(pairs, dtype=np.int64).reshape(-1, 2)
    pairs = pairs[pairs[:, 1] > 0]
    if len(pairs) == 0:
        return np.empty(0, dtype=np.int64)
    starts, counts = pairs[:, 0], pairs[:, 1]
    steps = np.ones(int(counts.sum()), dtype=np.int64)
    steps[0] = starts[0]
    jumps = np.cumsum(counts)[:-1]
    steps[jumps] = starts[1:] - (starts[:-1] + counts[:-1] - 1)
    return np.cumsum(steps)


def _set_tag(tstt, name: str, set_start: int, n_sets: int):
    """(set ids, values) of a tag on entity sets, from its dense or sparse storage."""
    sets = tstt["sets"]
    if "tags" in sets and name in sets["tags"]:
        return np.arange(set_start, set_start + n_sets, dtype=np.int64), sets["tags"][name][()]
    if "tags" not in tstt or name not in tstt["tags"] or "id_list" not in tstt["tags"][name]:
        return np.empty(0, dtype=np.int64), np.empty(0)
    group = tstt["tags"][name]
    ids = group["id_list"][()].astype(np.int64)
    values = group["values"][()]
    keep = (ids >= set_start) & (ids < set_start + n_sets)
    return ids[keep], values[keep]


def _faceting_tolerance(tstt, set_start, n_sets) -> Optional[float]:
    if "tags" not in tstt or "FACETING_TOL" not in tstt["tags"]:
        return None
    ids, values = _set_tag(tstt, "FACETING_TOL", set_start, n_sets)
    positive = np.asarray(values, dtype=float)
    positive = positive[positive > 0] if positive.size else positive
    if positive.size:
        return float(positive[0])
    value = tstt["tags"]["FACETING_TOL"].attrs.get("global")
    return float(value) if value is not None and float(value) > 0 else None


def read_h5m(filename) -> MeshedModel:
    """Read a DAGMC h5m-file into a MeshedModel.

    Volumes and surfaces keep the order they have in the file. The vertices are
    returned as stored: files written through MOAB from one STL-file per surface
    hold a separate copy of the vertices on every surface.
    """
    with h5py.File(filename, "r") as f:
        tstt = f["tstt"]
        coordinates = tstt["nodes/coordinates"]
        node_start = int(coordinates.attrs["start_id"])
        vertices = coordinates[()]

        if "elements" in tstt and "Tri3" in tstt["elements"]:
            conn = tstt["elements/Tri3/connectivity"]
            tri_start = int(conn.attrs["start_id"])
            connectivity = conn[()].astype(np.int64) - node_start
        else:
            tri_start = 0
            connectivity = np.empty((0, 3), dtype=np.int64)
        n_tri = len(connectivity)

        sets = tstt["sets"]
        set_list = sets["list"][()].astype(np.int64)
        set_start = int(sets["list"].attrs["start_id"])
        n_sets = len(set_list)
        contents = sets["contents"][()].astype(np.int64) if "contents" in sets else np.empty(0, dtype=np.int64)
        content_end = set_list[:, 0] + 1
        content_begin = np.concatenate(([0], content_end[:-1]))
        flags = set_list[:, 3]

        def set_contents(i):
            raw = contents[content_begin[i]:content_end[i]]
            return _expand_ranges(raw) if flags[i] & SET_RANGE_BIT else raw

        cat_ids, cat_values = _set_tag(tstt, "CATEGORY", set_start, n_sets)
        category = {int(i) - set_start: _opaque_to_str(v) for i, v in zip(cat_ids, cat_values)}
        dim_ids, dim_values = _set_tag(tstt, "GEOM_DIMENSION", set_start, n_sets)
        dimension = {int(i) - set_start: int(v) for i, v in zip(dim_ids, dim_values)}

        def sets_of(name, dim):
            found = [i for i in range(n_sets) if category.get(i) == name]
            if not found:
                found = [i for i in range(n_sets) if dimension.get(i) == dim and i not in category]
            return found

        volume_sets = sets_of("Volume", 3)
        surface_sets = sets_of("Surface", 2)
        group_sets = [i for i in range(n_sets) if category.get(i) == "Group"]
        volume_index = {i + set_start: k for k, i in enumerate(volume_sets)}

        gid_ids, gid_values = _set_tag(tstt, "GLOBAL_ID", set_start, n_sets)
        global_id = {int(i) - set_start: int(v) for i, v in zip(gid_ids, gid_values)}

        triangles = []
        offsets = [0]
        for i in surface_sets:
            ids = set_contents(i)
            tri = ids[(ids >= tri_start) & (ids < tri_start + n_tri)] - tri_start
            triangles.append(connectivity[tri])
            offsets.append(offsets[-1] + len(tri))

        senses = np.full((len(surface_sets), 2), NO_VOLUME, dtype=np.int64)
        sense_ids, sense_values = _set_tag(tstt, "GEOM_SENSE_2", set_start, n_sets)
        surface_index = {i + set_start: k for k, i in enumerate(surface_sets)}
        for set_id, pair in zip(sense_ids, np.asarray(sense_values).reshape(-1, 2)):
            k = surface_index.get(int(set_id))
            if k is not None:
                senses[k] = [volume_index.get(int(pair[0]), NO_VOLUME), volume_index.get(int(pair[1]), NO_VOLUME)]

        name_ids, name_values = _set_tag(tstt, "NAME", set_start, n_sets)
        names = {int(i) - set_start: _opaque_to_str(v) for i, v in zip(name_ids, name_values)}
        tags = [""] * len(volume_sets)
        implicit_complement = None
        other_groups = []
        for i in group_sets:
            name = names.get(i, "")
            if name.startswith(MATERIAL_PREFIX):
                material = name[len(MATERIAL_PREFIX):]
            elif name == "reflective":
                material = name
            else:
                other_groups.append(name)
                continue
            if material.endswith(COMPLEMENT_SUFFIX):
                implicit_complement = material[: -len(COMPLEMENT_SUFFIX)]
                continue
            for set_id in set_contents(i):
                k = volume_index.get(int(set_id))
                if k is not None:
                    tags[k] = material

        tolerance = _faceting_tolerance(tstt, set_start, n_sets)

    return MeshedModel(
        vertices=vertices,
        triangles=np.concatenate(triangles) if triangles else np.empty((0, 3), dtype=np.int64),
        surface_offsets=np.asarray(offsets, dtype=np.int64),
        surface_senses=senses,
        volume_tags=tags,
        implicit_complement=implicit_complement,
        faceting_tolerance=tolerance,
        volume_ids=[global_id.get(i, k + 1) for k, i in enumerate(volume_sets)],
        surface_ids=[global_id.get(i, k + 1) for k, i in enumerate(surface_sets)],
        info={"file": os.fspath(filename), "other_groups": other_groups},
    )


def group_name(tag: str) -> str:
    """Name of the DAGMC group that assigns a material tag to a volume."""
    # 'reflective' is a boundary condition, which must not get the material prefix
    return tag if tag == "reflective" else MATERIAL_PREFIX + tag


def _opaque(strings) -> np.ndarray:
    """Encode strings as the null padded 32 byte values of the NAME and CATEGORY tags."""
    out = np.zeros(len(strings), dtype=f"S{_NAME_SIZE}")
    for i, text in enumerate(strings):
        raw = text.encode("ascii")
        if len(raw) > _NAME_SIZE:
            raise ValueError(
                f"'{text}' is longer than the {_NAME_SIZE} characters a DAGMC group name can hold. "
                "Use a shorter material tag."
            )
        out[i] = raw
    return out.view(f"V{_NAME_SIZE}")


def _create_tag(tags, name, dtype, tag_class, default=None, **attributes):
    group = tags.create_group(name)
    group["type"] = np.dtype(dtype)
    group.attrs.create("class", tag_class, dtype=np.int32)
    if default is not None:
        group.attrs.create("default", default, dtype=group["type"])
        group.attrs.create("global", default, dtype=group["type"])
    for key, value in attributes.items():
        group.attrs.create(key, value, dtype=np.int32)
    return group


def _sparse_data(group, ids, values):
    group.create_dataset("id_list", data=np.asarray(ids, dtype=np.uint64))
    group.create_dataset("values", data=values)


def write_h5m(model: MeshedModel, filename, creator: str = "CAD_to_OpenMC") -> str:
    """Write a MeshedModel as a DAGMC h5m-file.

    The layout is the one MOAB writes: every volume and surface is an entity
    set, a surface is the child of the volumes it bounds, and every volume is
    the single member of a group that carries its material tag. Vertices are
    written as they are, so surfaces that share vertices in the model share
    them in the file.
    """
    filename = os.fspath(filename)
    n_nodes, n_tri = model.n_vertices, model.n_triangles
    n_vol, n_surf = model.n_volumes, model.n_surfaces
    has_complement = model.implicit_complement is not None
    if has_complement and n_vol == 0:
        raise ValueError("an implicit complement needs at least one volume")

    tri_start = n_nodes + 1
    set_start = tri_start + n_tri
    volume_set = set_start + np.arange(n_vol, dtype=np.int64)
    surface_set = set_start + n_vol + np.arange(n_surf, dtype=np.int64)
    group_set = set_start + n_vol + n_surf + np.arange(n_vol, dtype=np.int64)
    n_groups = n_vol + int(has_complement)
    n_sets = n_vol + n_surf + n_groups + 1
    file_set = set_start + n_sets - 1

    senses = model.surface_senses
    has_volume = senses != NO_VOLUME
    sense_sets = np.zeros((n_surf, 2), dtype=np.uint64)
    sense_sets[has_volume] = volume_set[senses[has_volume]]

    # a surface is the child of each volume it bounds, and has those volumes as parents
    surface_of_pair = np.repeat(np.arange(n_surf), 2).reshape(n_surf, 2)[has_volume]
    volume_of_pair = senses[has_volume]
    order = np.lexsort((surface_of_pair, volume_of_pair))
    children = surface_set[surface_of_pair[order]]
    parents = sense_sets[has_volume]

    counts = np.diff(model.surface_offsets)
    filled = counts > 0
    surface_ranges = np.stack([tri_start + model.surface_offsets[:-1], counts], axis=1)[filled]
    contents = np.concatenate([
        surface_ranges.ravel(),
        volume_set,
        volume_set[-1:] if has_complement else np.empty(0, dtype=np.int64),
        [1, file_set - 1],
    ])

    def per_set(volumes=0, surfaces=0, groups=0, file=0):
        return np.concatenate([
            np.broadcast_to(volumes, n_vol), np.broadcast_to(surfaces, n_surf),
            np.broadcast_to(groups, n_groups), [file],
        ]).astype(np.int64)

    set_list = np.stack([
        np.cumsum(per_set(surfaces=2 * filled, groups=1, file=2)) - 1,
        np.cumsum(per_set(volumes=np.bincount(volume_of_pair, minlength=n_vol))) - 1,
        np.cumsum(per_set(surfaces=has_volume.sum(axis=1))) - 1,
        per_set(volumes=_SET, surfaces=_SET_RANGES, groups=_SET, file=_SET_RANGES),
    ], axis=1)

    set_global_id = np.full(n_sets, -1, dtype=np.int32)
    set_global_id[:n_vol] = model.volume_ids
    set_global_id[n_vol:n_vol + n_surf] = model.surface_ids

    names = [group_name(tag) for tag in model.volume_tags]
    if has_complement:
        names.append(MATERIAL_PREFIX + model.implicit_complement + COMPLEMENT_SUFFIX)
    geometry_sets = np.arange(set_start, file_set, dtype=np.uint64)

    with h5py.File(filename, "w") as f:
        tstt = f.create_group("tstt")
        tstt.attrs.create("max_id", np.uint64(file_set))
        tstt["elemtypes"] = h5py.enum_dtype(_ELEMENT_TYPES, basetype=np.uint8)
        stamp = time.localtime()
        history = [creator, _package_version(), time.strftime("%m/%d/%y", stamp), time.strftime("%H:%M:%S", stamp)]
        tstt.create_dataset("history", data=[h.encode("ascii") for h in history], dtype=h5py.string_dtype("ascii"))

        nodes = tstt.create_group("nodes")
        coordinates = nodes.create_dataset("coordinates", data=model.vertices)
        coordinates.attrs.create("start_id", 1, dtype=np.int64)
        nodes.create_group("tags").create_dataset("GLOBAL_ID", data=np.full(n_nodes, -1, dtype=np.int32))

        tri3 = tstt.create_group("elements").create_group("Tri3")
        tri3.attrs.create("element_type", _ELEMENT_TYPES["Tri"], dtype=tstt["elemtypes"])
        connectivity = tri3.create_dataset("connectivity", data=model.triangles + 1, dtype=np.uint64)
        connectivity.attrs.create("start_id", tri_start, dtype=np.int64)
        tri3.create_group("tags").create_dataset("GLOBAL_ID", data=np.full(n_tri, -1, dtype=np.int32))

        sets = tstt.create_group("sets")
        sets.create_dataset("list", data=set_list).attrs.create("start_id", set_start, dtype=np.int64)
        sets.create_dataset("contents", data=contents.astype(np.uint64))
        sets.create_dataset("children", data=children.astype(np.uint64))
        sets.create_dataset("parents", data=parents.astype(np.uint64))
        sets.create_group("tags").create_dataset("GLOBAL_ID", data=set_global_id)

        tags = tstt.create_group("tags")
        opaque = np.dtype(f"V{_NAME_SIZE}")
        _sparse_data(
            _create_tag(tags, "CATEGORY", opaque, 1),
            geometry_sets,
            _opaque(["Volume"] * n_vol + ["Surface"] * n_surf + ["Group"] * n_groups),
        )
        _sparse_data(
            _create_tag(tags, "GEOM_DIMENSION", np.int32, 1, default=-1),
            geometry_sets,
            np.concatenate([np.full(n_vol, 3), np.full(n_surf, 2), np.full(n_groups, 4)]).astype(np.int32),
        )
        _create_tag(tags, "GLOBAL_ID", np.int32, 2, default=-1)
        _sparse_data(_create_tag(tags, "NAME", opaque, 1), group_set_ids(group_set, has_complement), _opaque(names))
        for name in ("DIRICHLET_SET", "MATERIAL_SET", "NEUMANN_SET"):
            _create_tag(tags, name, np.int32, 1, default=-1)

        # the two volumes of a surface are stored as an array of two entity handles
        sense = _create_tag(tags, "GEOM_SENSE_2", np.dtype("(2,)u8"), 1, is_handle=1)
        sense.create_dataset("id_list", data=surface_set.astype(np.uint64))
        pair_type = h5py.h5t.array_create(h5py.h5t.NATIVE_UINT64, (2,))
        values = h5py.h5d.create(sense.id, b"values", pair_type, h5py.h5s.create_simple((n_surf,)))
        if n_surf:
            values.write(h5py.h5s.ALL, h5py.h5s.ALL, np.ascontiguousarray(sense_sets), mtype=pair_type)
        values.close()

        if model.faceting_tolerance is not None:
            # A sparse tag on the file set only, which is where DAGMC and
            # make_watertight look for it. make_watertight rejects files in
            # which more than one set carries this tag.
            tolerance = _create_tag(tags, "FACETING_TOL", np.float64, 1)
            _sparse_data(tolerance, [file_set], np.array([model.faceting_tolerance], dtype=np.float64))
    return filename


def group_set_ids(group_set, has_complement) -> np.ndarray:
    """Ids of the group sets, including the one for the implicit complement."""
    if has_complement:
        return np.append(group_set, group_set[-1] + 1).astype(np.uint64)
    return group_set.astype(np.uint64)


def _package_version() -> str:
    from importlib import metadata

    try:
        return metadata.version("CAD_to_OpenMC")
    except metadata.PackageNotFoundError:
        return "unknown"
