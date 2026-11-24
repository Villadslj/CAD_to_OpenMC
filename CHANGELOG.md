# unreleased changes since 1.0.1
- the 'gmsh' backend no longer changes the length unit of the OCC session. With
gmsh from conda-forge, which shares OCC with cadquery, every later import or
export was 10 times off. Step-files are now always read in mm.
- a second 'gmsh' run in the same python session meshed the solids of the first
run again (or none at all). The mesher now loads the new solids, and closes the
gmsh session it opened when it is done.
- test_file_in_other_length_units_comes_in_as_cm works with OCC 7.8, which does
not convert the coordinates when it writes a step-file in other units.
- the 'stl2' and 'db' backends now triangulate all solids together and keep the
mesh in memory, instead of writing one stl-file per face. Every edge is divided
once, so the surfaces of a volume always fit together, whatever the order of the
solids. Before, a solid could be left open when a larger neighbour was meshed
first, which lost particles in transport.
- the mesh of 'stl2' and 'db' is checked before it is written: an error is raised
when a volume is not closed (mesher_config['strict']). The outcome is kept in
Assembly.validation_report, and the mesh itself in Assembly.meshed_model.
- mesher_config['threads'] now takes effect for 'stl2' and 'db', and the mesh does
not depend on it. The default uses all available cores.
- the h5m-file of 'stl2' and 'db' is written with h5py. pymoab is only needed for
the backends that go through stl-files ('stl', 'gmsh', 'stl2-legacy',
'db-legacy') and for H5MTransformer.
- 'stl2' with a relative tolerance can give more triangles than before, see
mesher_config['relative_tolerance_factor'] and mesher_config['mesh_grouping'].
- new mesher_config entries: tolerance_mode, mesh_grouping,
relative_tolerance_factor, strict, volume_tolerance, vtk.
- the previous implementations remain available as the backends 'stl2-legacy'
and 'db-legacy'.
- new: python -m CAD_to_OpenMC.validate file.h5m checks any DAGMC h5m-file.
- the names of the parts, which the material tags are taken from, are read from
the step-file together with the shapes. The second import of the file with gmsh
is gone, which took most of the import time for files with many parts. gmsh is
only needed for the 'gmsh' backend. Assembly.names_from = 'gmsh' brings the
previous way of finding the names back.
- all solids of a part that consists of several bodies get the tag of the part.
They used to get the default tag.
- vol_skip no longer shifts the tags: the volumes that are kept keep their own.
- merge_all takes the solid that replaces every part from the record
OpenCASCADE keeps of the merge, instead of comparing volumes, bounding boxes and
centres of all solids with each other. That comparison took most of the time of
the merge for models with many parts.
- where the merge finds that solids overlap, the region they have in common is
given to the smallest of them and a warning is printed. Nothing is left out;
before, the pieces that were left over were dropped. The overlaps are kept in
Assembly.overlaps.
- new: Assembly.find_overlaps() looks for all solids that overlap, also those
the merge does not notice, and gives the volume of the regions.
- merge_all raises MergeError when OpenCASCADE reports that it failed.
- merge2h5m joins the meshes of assemblies meshed with 'stl2' or 'db' in memory.
It failed before, as the stl-files of the assemblies were not found.
- fixes: settings of an earlier run (refine, threads) no longer leak into the
next run in the same process; get_all_tags and get_unique_tags work; the working
directory is restored when meshing fails; a gmsh module that cannot load its
libraries no longer prevents importing the package.

# changes from 0.13.3 to x.y.z
- fixing the logic of tagging. If a tag ditionary is supplied this will
take precedence over extracted tags, but defaults to the extracted tag if
no match is found in the dictionary
- improvements to the imprinting logic, following the logic in the inbound
in cadquery
