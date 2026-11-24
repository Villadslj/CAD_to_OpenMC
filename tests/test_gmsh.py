import pytest
import CAD_to_OpenMC.assembly as ab
from tests.harnessRun import HarnessRun
import pathlib as pl

class HarnessGmsh(HarnessRun):
  def run(self):
    print(f'gmsh: {self.a.stp_files})')
    self.a.merge_all()
    ab.mesher_config['refine']=False
    h5p = pl.Path('out_gmsh.h5m')
    self.a.solids_to_h5m(backend='gmsh',h5m_filename=str(h5p))
    assert h5p.exists()
    assert self.is_validh5m(h5p)
    #self.cleanup()

def testgmsh():
  t = HarnessGmsh()
  t.run()

def test_second_gmsh_run_meshes_its_own_solids(tmp_path, monkeypatch):
  import cadquery as cq
  monkeypatch.chdir(tmp_path)
  cq.exporters.export(cq.Workplane("XY").box(10, 20, 30), "one.step")
  two = cq.Workplane("XY").box(10, 20, 30).union(cq.Workplane("XY").box(5, 5, 5).translate((20, 0, 0)))
  cq.exporters.export(two, "two.step")
  for name, n in (("two", 2), ("one", 1)):
    a = ab.Assembly([f"{name}.step"], verbose=0)
    a.run(backend="gmsh", h5m_filename=f"{name}.h5m")
    assert len(a.entities) == n
    assert all(hasattr(e, "stl") for e in a.entities)
