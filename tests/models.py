"""Synthetic CAD models for tests and benchmarks.

Every generator returns a list of (part name, cadquery solid). The part name
starts with the material tag, the way CAD_to_OpenMC extracts tags from step
files. Dimensions are in mm, the unit of a step file.
"""
import math

import cadquery as cq


def pellet_stack(n_pellets=3, radius=4.0, height=10.0, wall=1.0, cladding_first=True):
    """A cladding tube filled with a stack of touching pellets.

    The pellets share their lateral faces with the cladding and their end faces
    with each other. The curved edges between them span the pellets but only a
    fraction of the cladding, which makes this the test case for meshing that
    depends on the size of the solid.
    """
    pellets = [
        (f"fuel_{i}", cq.Workplane("XY").workplane(offset=i * height).circle(radius).extrude(height).val())
        for i in range(n_pellets)
    ]
    cladding = (
        "clad_tube",
        cq.Workplane("XY").circle(radius + wall).circle(radius).extrude(n_pellets * height).val(),
    )
    return [cladding] + pellets if cladding_first else pellets + [cladding]


def pin_lattice(nx=3, ny=None, pitch=12.6, r_fuel=4.1, r_clad=4.75, height=40.0):
    """nx x ny pin cells, each made of a fuel rod, a cladding tube and a coolant box."""
    ny = nx if ny is None else ny
    parts = []
    for i in range(nx):
        for j in range(ny):
            x, y = i * pitch, j * pitch
            parts.append((f"fuel_{i}_{j}", cq.Workplane("XY").center(x, y).circle(r_fuel).extrude(height).val()))
            parts.append(
                (f"clad_{i}_{j}", cq.Workplane("XY").center(x, y).circle(r_clad).circle(r_fuel).extrude(height).val())
            )
            parts.append(
                (f"water_{i}_{j}", cq.Workplane("XY").center(x, y).rect(pitch, pitch).circle(r_clad).extrude(height).val())
            )
    return parts


def nested_shells(n_shells=3, r_inner=5.0, thickness=2.0):
    """A solid sphere surrounded by concentric spherical shells."""
    parts = [("core_0", cq.Workplane("XY").sphere(r_inner).val())]
    for i in range(n_shells):
        r0 = r_inner + i * thickness
        shell = cq.Workplane("XY").sphere(r0 + thickness).cut(cq.Workplane("XY").sphere(r0)).val()
        parts.append((f"shell_{i + 1}", shell))
    return parts


def tiny_on_huge(big=1000.0, small=2.0):
    """A small cylinder standing on a large block."""
    block = cq.Workplane("XY").box(big, big, big / 10, centered=(True, True, False)).val()
    stud = cq.Workplane("XY").workplane(offset=big / 10).circle(small).extrude(2 * small).val()
    return [("steel_block", block), ("copper_stud", stud)]


def separate_boxes(n=3, size=10.0, gap=5.0):
    """Boxes that do not touch each other."""
    return [
        (f"iron_{i}", cq.Workplane("XY").center(i * (size + gap), 0).box(size, size, size).val())
        for i in range(n)
    ]


def instanced_pin_lattice(nx=2, ny=2, angle=30.0, pitch=12.6, r_fuel=4.1, r_clad=4.75, height=40.0):
    """A pin lattice built the way CAD assemblies are: one pin cell, placed nx x ny times.

    The three solids of the cell exist once, around the origin. Every copy is
    put in place by a location (a rotation about the z-axis and a translation)
    of the assembly, so the faces of the imported solids carry locations, and
    neighbouring cells meet at faces that belong to differently placed copies.
    Returns (part name, solid, location) triples.
    """
    fuel = cq.Workplane("XY").circle(r_fuel).extrude(height).val()
    clad = cq.Workplane("XY").circle(r_clad).circle(r_fuel).extrude(height).val()
    water = cq.Workplane("XY").rect(pitch, pitch).circle(r_clad).extrude(height).val()
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    parts = []
    for i in range(nx):
        for j in range(ny):
            x, y = i * pitch, j * pitch
            location = cq.Location(cq.Vector(c * x - s * y, s * x + c * y, 0.0), cq.Vector(0, 0, 1), angle)
            parts += [
                (f"fuel_{i}_{j}", fuel, location),
                (f"clad_{i}_{j}", clad, location),
                (f"water_{i}_{j}", water, location),
            ]
    return parts


def write_step(parts, filename):
    """Write the parts to a step file, each as a named product.

    A part is (name, solid) or (name, solid, location of the solid in the assembly).
    """
    assy = cq.Assembly(name="model")
    for name, solid, *location in parts:
        assy.add(solid, name=name, loc=location[0] if location else None)
    assy.export(str(filename))
    return str(filename)
