import pathlib
import os
import math
import sys
import pytest
#if openmc cannot be imported - skip these tests
openmc = pytest.importorskip("openmc")

import CAD_to_OpenMC.assembly as ab

from tests.OMC_DAGMC_run import DAGMC_template
from tests.harnessRun import HarnessRun

class OMC_DAGMC_harness(HarnessRun):
  def __init__(self, step):
    self.path = pathlib.Path(step)
    self.h5m = pathlib.Path(self.path.name).with_suffix('.h5m')
    self.nuclear_lib = pathlib.Path('tests/nuclear_data_testlib/cross_sections.xml').absolute()
    self.aa = ab.Assembly([str(self.path)], verbose = 2)
    self.tt = DAGMC_template(self.h5m)

  def run(self):
    self.aa.run(backend='stl2', merge = True, h5m_filename = self.h5m)
    assert self.h5m.exists()
    self.tt.run()
    self.tt.check_results()
    self.tt.cleanup()
    self.cleanup()

#@pytest.mark.skipif(sys.version_info >= (3, 11), reason="requires python3.10 or lower")
def test_h5m_neutronics_p1():
  o = OMC_DAGMC_harness('examples/step_files/pincell1.step')
  openmc.config['cross_sections']=str(o.nuclear_lib)
  o.tt.results={'keff':(0.07944,0.00070)}
  o.run()

#@pytest.mark.skipif(sys.version_info >= (3, 11), reason="requires python3.10 or lower")
def test_h5m_neutronics_p2():
  o = OMC_DAGMC_harness('examples/step_files/pincell2.step')
  openmc.config['cross_sections']=str(o.nuclear_lib)
  o.tt.results={'keff':(0.07786,0.0008)}
  o.run()

#@pytest.mark.skipif(sys.version_info >= (3, 11), reason="requires python3.10 or lower")
def test_h5m_neutronics_tors():
  o = OMC_DAGMC_harness('examples/step_files/toroids.step')
  # override source spatial distribution
  o.tt.settings.source.space=openmc.stats.CylindricalIndependent(
    openmc.stats.Discrete([100,85,77.5],[1.0/3.0,1.0/3.0, 1.0/3.0]), openmc.stats.Uniform(0.0,2.0*math.pi), openmc.stats.Discrete([0.0],[1.0])
  )
  openmc.config['cross_sections']=str(o.nuclear_lib)
  o.tt.results={'keff':(1.61936,0.08562)}
  o.run()

#@pytest.mark.skipif(sys.version_info >= (3, 11), reason="requires python3.10 or lower")
def test_h5m_neutronics_spheroids():
  o = OMC_DAGMC_harness('examples/step_files/spheroids.step')
  openmc.config['cross_sections']=str(o.nuclear_lib)
  o.tt.results={'keff':(1.39082,0.01622)}
  o.run()

#@pytest.mark.skipif(sys.version_info >= (3, 10), reason="requires python3.10 or lower")
def test_h5m_neutronics_ellipsoids():
  o = OMC_DAGMC_harness('examples/step_files/oblate_ellipsoids.step')
  openmc.config['cross_sections']=str(o.nuclear_lib)
  o.tt.results={'keff':(1.05396,0.01514)}
  o.run()

def test_no_particle_gets_lost_in_a_stack_of_pellets(tmp_path, monkeypatch, capfd):
  # The cladding comes before the pellets it holds. Meshed solid by solid in that order, with
  # these tolerances, the cladding was left open and OpenMC lost particles in it.
  from tests import models
  nuclear_lib = pathlib.Path('tests/nuclear_data_testlib/cross_sections.xml').absolute()
  monkeypatch.chdir(tmp_path)
  for key, value in (('tolerance', 0.001), ('angular_tolerance', 0.2)):
    monkeypatch.setitem(ab.mesher_config, key, value)
  parts = [(name.replace('fuel', 'uo2').replace('clad', 'zirconium'), solid) for name, solid in models.pellet_stack()]
  models.write_step(parts, 'pellets.step')
  aa = ab.Assembly(['pellets.step'], verbose = 0)
  aa.run(backend='stl2', merge = True, h5m_filename = 'pellets.h5m')

  openmc.config['cross_sections']=str(nuclear_lib)
  tt = DAGMC_template(pathlib.Path('pellets.h5m'))
  tt.settings.source.space=openmc.stats.Point((0.0, 0.0, 1.5))
  tt.run()
  output = capfd.readouterr().out
  assert 'No intersection found' not in output
  assert 'could not be located' not in output
  tt.check_results()

if __name__=='__main__':
  test_h5m_neutronics_p1()
  test_h5m_neutronics_p2()
  test_h5m_neutronics_tors()
  test_h5m_neutronics_spheroids()
  test_h5m_neutronics_ellipsoids()
