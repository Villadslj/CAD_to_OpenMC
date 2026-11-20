"""The h5m writer: what is written has to come back unchanged, and MOAB has to understand it."""
import numpy as np
import pytest

from CAD_to_OpenMC.h5m_io import read_h5m, write_h5m
from tests.test_validate import two_tetrahedra


@pytest.fixture
def model():
    """Two volumes that share a surface, with everything set that ends up in the file."""
    model = two_tetrahedra(shared=True)
    model.volume_tags = ["steel", "reflective"]
    model.volume_ids = np.array([7, 9])
    model.surface_ids = np.array([3, 5, 8])
    model.implicit_complement = "water"
    model.faceting_tolerance = 0.01
    return model


def test_model_survives_writing_and_reading(model, tmp_path):
    back = read_h5m(write_h5m(model, tmp_path / "model.h5m"))

    for name in ("vertices", "triangles", "surface_offsets", "surface_senses", "volume_ids", "surface_ids"):
        assert np.array_equal(getattr(back, name), getattr(model, name)), name
    assert back.volume_tags == ["steel", "reflective"]
    assert back.implicit_complement == "water"
    assert back.faceting_tolerance == 0.01


def test_material_tag_that_does_not_fit_in_a_group_name_is_refused(model, tmp_path):
    model.volume_tags[0] = "a_material_tag_of_thirty_chars"
    with pytest.raises(ValueError, match="longer than the 32 characters"):
        write_h5m(model, tmp_path / "model.h5m")


def test_moab_finds_the_dagmc_structure(model, tmp_path):
    pytest.importorskip("pymoab")
    from pymoab import core, types

    mb = core.Core()
    mb.load_file(write_h5m(model, tmp_path / "model.h5m"))

    def tag(name, size, kind):
        return mb.tag_get_handle(name, size, kind, types.MB_TAG_SPARSE, create_if_missing=False)

    category = tag(types.CATEGORY_TAG_NAME, types.CATEGORY_TAG_SIZE, types.MB_TYPE_OPAQUE)
    sets = {"Volume": [], "Surface": [], "Group": []}
    for entity_set in mb.get_entities_by_type(0, types.MBENTITYSET):
        try:
            sets[mb.tag_get_data(category, entity_set, flat=True)[0]].append(entity_set)
        except RuntimeError:
            pass  # the set that holds the whole file has no category
    volumes, surfaces, groups = sets["Volume"], sets["Surface"], sets["Group"]

    assert (len(volumes), len(surfaces), len(groups)) == (2, 3, 3)
    assert [list(mb.get_child_meshsets(v)) for v in volumes] == [surfaces[:2], [surfaces[0], surfaces[2]]]
    assert [len(mb.get_entities_by_type(s, types.MBTRI)) for s in surfaces] == [1, 3, 3]
    sense = tag("GEOM_SENSE_2", 2, types.MB_TYPE_HANDLE)
    assert mb.tag_get_data(sense, surfaces).tolist() == [[volumes[0], volumes[1]], [volumes[0], 0], [volumes[1], 0]]
    name = tag(types.NAME_TAG_NAME, types.NAME_TAG_SIZE, types.MB_TYPE_OPAQUE)
    assert [mb.tag_get_data(name, g, flat=True)[0] for g in groups] == ["mat:steel", "reflective", "mat:water_comp"]
    assert [list(mb.get_entities_by_handle(g)) for g in groups] == [[volumes[0]], [volumes[1]], [volumes[1]]]
    global_id = mb.tag_get_handle(types.GLOBAL_ID_TAG_NAME)
    assert mb.tag_get_data(global_id, volumes + surfaces, flat=True).tolist() == [7, 9, 3, 5, 8]
