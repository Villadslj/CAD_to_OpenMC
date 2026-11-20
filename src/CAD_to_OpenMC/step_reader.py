"""Reading of step-files: the shapes together with the names of the parts.

The file is read once, into an OpenCASCADE document (XDE). The shapes are the
ones cadquery's importStep returns. The names are those of the products in the
step-file, which is where the material tags come from.
"""
from __future__ import annotations

from typing import List

from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool


def _name(label: TDF_Label) -> str:
    attribute = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
        return attribute.Get().ToExtString()
    return ""


def _solids(shape: TopoDS_Shape) -> TopTools_IndexedMapOfShape:
    solids = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)
    return solids


class StepFile:
    """The contents of a step-file.

    Attributes
    ----------
    shapes : list of TopoDS_Shape
        The shapes in the file, one for every root that is transferred.
    """

    def __init__(self, filename):
        self.filename = str(filename)
        # the document has to stay alive for as long as its labels are used
        self._document = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        for switch_off in (reader.SetColorMode, reader.SetLayerMode, reader.SetSHUOMode, reader.SetGDTMode):
            switch_off(False)
        if reader.ReadFile(self.filename) != IFSelect_RetDone:
            raise ValueError(f"STEP File could not be loaded: {self.filename}")
        if not reader.Transfer(self._document):
            raise ValueError(f"STEP File could not be transferred: {self.filename}")
        plain = reader.Reader()
        self.shapes: List[TopoDS_Shape] = [plain.Shape(i + 1) for i in range(plain.NbShapes())]

        # the name of every solid, found by following the assembly structure down to the parts
        self._named = TopTools_IndexedMapOfShape()
        self._names: List[str] = []
        roots = TDF_LabelSequence()
        XCAFDoc_DocumentTool.ShapeTool_s(self._document.Main()).GetFreeShapes(roots)
        for i in range(roots.Length()):
            self._visit(roots.Value(i + 1), TopLoc_Location(), "")

    def _visit(self, label: TDF_Label, location: TopLoc_Location, inherited: str) -> None:
        name = _name(label) or inherited
        if XCAFDoc_ShapeTool.IsReference_s(label):
            # an instance: the part it refers to, put in place
            referred = TDF_Label()
            XCAFDoc_ShapeTool.GetReferredShape_s(label, referred)
            self._visit(referred, location.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(label)), name)
        elif XCAFDoc_ShapeTool.IsAssembly_s(label):
            components = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(label, components, False)
            for i in range(components.Length()):
                self._visit(components.Value(i + 1), location, name)
        else:
            solids = _solids(XCAFDoc_ShapeTool.GetShape_s(label).Moved(location))
            for i in range(solids.Extent()):
                if self._named.Add(solids.FindKey(i + 1)) > len(self._names):
                    self._names.append(name)

    def solid_name(self, solid: TopoDS_Shape) -> str:
        """Name of the part a solid of one of the shapes belongs to, '' if it has none."""
        index = self._named.FindIndex(solid)
        return self._names[index - 1] if index else ""

    def solid_names(self) -> List[str]:
        """The name of the part of every solid, in the order in which the solids of the shapes are found."""
        names = []
        for shape in self.shapes:
            solids = _solids(shape)
            names += [self.solid_name(solids.FindKey(i + 1)) for i in range(solids.Extent())]
        return names
