# unreleased changes since 1.0.1
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
