# -*- coding: utf-8 -*-
"""
Raphal Production Dashboard V19.15 - FreeCAD conversion worker.

Key change:
For mesh -> CAD exchange formats, use FreeCAD's native Shape.makeShapeFromMesh()
instead of creating one Part.Face per triangle in Python. Large meshes are
automatically decimated before CAD conversion to avoid FreeCAD exhausting RAM.

Called by:
  FreeCADCmd.exe freecad_converter.py INPUT OUTPUT LINEAR_DEFLECTION ANGULAR_DEFLECTION MAX_CAD_FACETS
"""
import os
import sys
import json
import traceback
from xml.sax.saxutils import escape as xml_escape

RESULT_PREFIX="RAPHAL_FREECAD_RESULT="

def result(ok, **kwargs):
    print(RESULT_PREFIX+json.dumps({"ok":bool(ok), **kwargs}, ensure_ascii=False), flush=True)

try:
    import FreeCAD as App
    import Part
    import Mesh
    import Import
except Exception as exc:
    result(False,error="Impossible de charger les modules FreeCAD: %s"%exc)
    raise

def ext(path):
    return os.path.splitext(path)[1].lower()

def shape_objects(doc):
    return [o for o in doc.Objects if hasattr(o,"Shape") and not o.Shape.isNull()]

def mesh_objects(doc):
    out=[]
    for o in doc.Objects:
        try:
            if hasattr(o,"Mesh") and o.Mesh.CountFacets>0:
                out.append(o)
        except Exception:
            pass
    return out

def doc_objects(doc):
    return [o for o in doc.Objects if hasattr(o,"Shape") or hasattr(o,"Mesh")]

def import_file(path):
    e=ext(path)
    if e==".fcstd":
        return App.openDocument(path)

    doc=App.newDocument("RaphalConvert")
    if e in (".stl",".obj",".ply",".off",".amf"):
        Mesh.insert(path,doc.Name)
    elif e==".3mf":
        # Prefer Mesh: some FreeCAD builds load a 3MF as a Part shape, which
        # makes mesh exports and safe 2D projections unnecessarily expensive.
        imported=False
        try:
            Mesh.insert(path,doc.Name)
            imported=bool(mesh_objects(doc))
        except Exception:
            imported=False
        if not imported:
            Import.insert(path,doc.Name)
    elif e in (".step",".stp",".iges",".igs"):
        Import.insert(path,doc.Name)
    elif e in (".brep",".brp"):
        sh=Part.Shape()
        sh.read(path)
        obj=doc.addObject("Part::Feature","ImportedBREP")
        obj.Shape=sh
    elif e==".dxf":
        try:
            import importDXF
            importDXF.insert(path,doc.Name)
        except Exception:
            Import.insert(path,doc.Name)
    elif e==".svg":
        try:
            import importSVG
            importSVG.insert(path,doc.Name)
        except Exception:
            Import.insert(path,doc.Name)
    elif e==".dae":
        try:
            import importDAE
            importDAE.insert(path,doc.Name)
        except Exception:
            Import.insert(path,doc.Name)
    else:
        Import.insert(path,doc.Name)
    doc.recompute()
    return doc

def clone_and_reduce_mesh(mesh, max_facets):
    """Return a copy, decimated only when needed."""
    cp=Mesh.Mesh(mesh)
    before=int(cp.CountFacets)
    if max_facets and before>max_facets:
        try:
            cp.decimate(int(max_facets))
        except TypeError:
            # Older bindings expose tolerance/reduction overload.
            reduction=max(0.0,min(0.98,1.0-(float(max_facets)/float(before))))
            cp.decimate(0.0,reduction)
    return cp,before,int(cp.CountFacets)

def mesh_to_part_shape(mesh, tolerance):
    """
    Native FreeCAD conversion. This is far more memory-efficient than creating
    Python Part.Face objects for every triangle.
    """
    shape=Part.Shape()
    shape.makeShapeFromMesh(mesh.Topology,float(tolerance))
    if shape.isNull():
        raise RuntimeError("FreeCAD a produit une forme vide depuis le maillage.")

    # Remove splitters when possible, then attempt solid creation for closed shells.
    try:
        shape=shape.removeSplitter()
    except Exception:
        pass
    try:
        if shape.isClosed():
            solid=Part.makeSolid(shape)
            if solid and not solid.isNull():
                shape=solid
    except Exception:
        pass
    return shape

def build_shapes_from_meshes(doc,tolerance,max_facets):
    rebuilt=[]
    stats=[]
    for mo in mesh_objects(doc):
        reduced,before,after=clone_and_reduce_mesh(mo.Mesh,max_facets)
        sh=mesh_to_part_shape(reduced,tolerance)
        po=doc.addObject("Part::Feature","RebuiltMeshShape")
        po.Label=mo.Label+" (faceted CAD)"
        po.Shape=sh
        rebuilt.append(po)
        stats.append({"object":mo.Label,"facets_before":before,"facets_after":after})
    doc.recompute()
    return rebuilt,stats

def export_mesh(doc,path):
    objs=doc_objects(doc)
    if not objs:
        raise RuntimeError("Aucun objet exportable.")
    Mesh.export(objs,path)

def mesh_projection_edges(doc):
    """Return a deduplicated top-view wireframe for safe 3D -> 2D export."""
    points=[]
    edges=set()
    for mesh_object in mesh_objects(doc):
        vertices,facets=mesh_object.Mesh.Topology
        offset=len(points)
        points.extend((float(vertex.x),float(vertex.y)) for vertex in vertices)
        for facet in facets:
            if len(facet)<2:
                continue
            for index in range(len(facet)):
                first=offset+int(facet[index])
                second=offset+int(facet[(index+1)%len(facet)])
                if first!=second:
                    edges.add((first,second) if first<second else (second,first))
    if not points or not edges:
        raise RuntimeError("Le maillage ne contient aucune arête exportable.")
    return points,sorted(edges)

def export_mesh_projection_dxf(doc,path):
    points,edges=mesh_projection_edges(doc)
    with open(path,"w",encoding="ascii",errors="xmlcharrefreplace",newline="\n") as handle:
        handle.write("0\nSECTION\n2\nENTITIES\n")
        for first,second in edges:
            x1,y1=points[first]
            x2,y2=points[second]
            handle.write("0\nLINE\n8\n0\n10\n%.9g\n20\n%.9g\n30\n0\n11\n%.9g\n21\n%.9g\n31\n0\n"%(x1,y1,x2,y2))
        handle.write("0\nENDSEC\n0\nEOF\n")

def export_mesh_projection_svg(doc,path):
    points,edges=mesh_projection_edges(doc)
    xs=[point[0] for point in points]
    ys=[point[1] for point in points]
    min_x,max_x=min(xs),max(xs)
    min_y,max_y=min(ys),max(ys)
    width=max(max_x-min_x,1.0)
    height=max(max_y-min_y,1.0)
    commands=[]
    for first,second in edges:
        x1,y1=points[first]
        x2,y2=points[second]
        commands.append("M %.9g %.9g L %.9g %.9g"%(x1,y1,x2,y2))
    with open(path,"w",encoding="utf-8",newline="\n") as handle:
        handle.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.9g %.9g %.9g %.9g">\n'%(min_x,min_y,width,height))
        handle.write('<path fill="none" stroke="black" stroke-width="0.1" d="%s"/>\n'%xml_escape(" ".join(commands)))
        handle.write("</svg>\n")

def export_amf(doc,path):
    """Write AMF directly because this FreeCAD build cannot export it."""
    meshes=[]
    for mesh_object in mesh_objects(doc):
        vertices,facets=mesh_object.Mesh.Topology
        if vertices and facets:
            meshes.append((vertices,facets))
    if not meshes:
        raise RuntimeError("Aucun maillage AMF exportable.")
    with open(path,"w",encoding="utf-8",newline="\n") as handle:
        handle.write('<?xml version="1.0" encoding="UTF-8"?>\n<amf unit="millimeter">\n')
        for object_id,(vertices,facets) in enumerate(meshes):
            handle.write('<object id="%d"><mesh><vertices>\n'%object_id)
            for vertex in vertices:
                handle.write('<vertex><coordinates><x>%.9g</x><y>%.9g</y><z>%.9g</z></coordinates></vertex>\n'%(float(vertex.x),float(vertex.y),float(vertex.z)))
            handle.write("</vertices><volume>\n")
            for facet in facets:
                if len(facet)==3:
                    handle.write('<triangle><v1>%d</v1><v2>%d</v2><v3>%d</v3></triangle>\n'%(int(facet[0]),int(facet[1]),int(facet[2])))
            handle.write("</volume></mesh></object>\n")
        handle.write("</amf>\n")

def export_mesh_scene(doc,path,tolerance,scene_format):
    """Write portable DAE/Inventor/VRML files without FreeCAD GUI exporters."""
    points=[]
    facets=[]
    for mesh_object in mesh_objects(doc):
        vertices,triangles=mesh_object.Mesh.Topology
        offset=len(points)
        points.extend((float(vertex.x),float(vertex.y),float(vertex.z)) for vertex in vertices)
        facets.extend(tuple(offset+int(index) for index in triangle) for triangle in triangles if len(triangle)==3)
    if not points:
        for shape_object in shape_objects(doc):
            vertices,triangles=shape_object.Shape.tessellate(float(tolerance))
            offset=len(points)
            points.extend((float(vertex.x),float(vertex.y),float(vertex.z)) for vertex in vertices)
            facets.extend(tuple(offset+int(index) for index in triangle) for triangle in triangles if len(triangle)==3)
    if not points or not facets:
        raise RuntimeError("Aucun maillage exportable dans ce format.")

    if scene_format=="wrl":
        with open(path,"w",encoding="utf-8",newline="\n") as handle:
            handle.write("#VRML V2.0 utf8\nShape { geometry IndexedFaceSet {\ncoord Coordinate { point [\n")
            for x,y,z in points:
                handle.write("%.9g %.9g %.9g,\n"%(x,y,z))
            handle.write("] }\ncoordIndex [\n")
            for triangle in facets:
                handle.write("%d, %d, %d, -1,\n"%triangle)
            handle.write("]\n} }\n")
        return

    if scene_format=="iv":
        with open(path,"w",encoding="utf-8",newline="\n") as handle:
            handle.write("#Inventor V2.1 ascii\nSeparator {\nCoordinate3 { point [\n")
            for x,y,z in points:
                handle.write("%.9g %.9g %.9g,\n"%(x,y,z))
            handle.write("] }\nIndexedFaceSet { coordIndex [\n")
            for triangle in facets:
                handle.write("%d, %d, %d, -1,\n"%triangle)
            handle.write("] }\n}\n")
        return

    values=" ".join("%.9g %.9g %.9g"%point for point in points)
    indices=" ".join("%d %d %d"%triangle for triangle in facets)
    counts=" ".join("3" for _ in facets)
    with open(path,"w",encoding="utf-8",newline="\n") as handle:
        handle.write('<?xml version="1.0" encoding="utf-8"?>\n')
        handle.write('<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">\n')
        handle.write('<asset><unit name="millimeter" meter="0.001"/><up_axis>Z_UP</up_axis></asset>\n')
        handle.write('<library_geometries><geometry id="mesh" name="mesh"><mesh>\n')
        handle.write('<source id="mesh-positions"><float_array id="mesh-positions-array" count="%d">%s</float_array>'%(len(points)*3,values))
        handle.write('<technique_common><accessor source="#mesh-positions-array" count="%d" stride="3"/></technique_common></source>\n'%len(points))
        handle.write('<vertices id="mesh-vertices"><input semantic="POSITION" source="#mesh-positions"/></vertices>\n')
        handle.write('<polylist count="%d"><input semantic="VERTEX" source="#mesh-vertices" offset="0"/><vcount>%s</vcount><p>%s</p></polylist>\n'%(len(facets),counts,indices))
        handle.write('</mesh></geometry></library_geometries><library_visual_scenes><visual_scene id="Scene"><node id="mesh-node"><instance_geometry url="#mesh"/></node></visual_scene></library_visual_scenes><scene><instance_visual_scene url="#Scene"/></scene></COLLADA>\n')

def export_file(doc,path,tolerance,max_cad_facets):
    e=ext(path)
    shapes=shape_objects(doc)
    stats=[]

    if e==".fcstd":
        doc.recompute()
        doc.saveAs(path)
        return stats

    cad_out=e in (".step",".stp",".iges",".igs",".brep",".brp")
    if cad_out and not shapes:
        _,stats=build_shapes_from_meshes(doc,tolerance,max_cad_facets)
        shapes=shape_objects(doc)

    if e in (".step",".stp"):
        if not shapes:
            raise RuntimeError("Aucune forme STEP exportable.")
        Import.export(shapes,path)
        return stats
    if e in (".iges",".igs"):
        if not shapes:
            raise RuntimeError("Aucune forme IGES exportable.")
        Import.export(shapes,path)
        return stats
    if e in (".brep",".brp"):
        if not shapes:
            raise RuntimeError("Aucune forme BREP exportable.")
        if len(shapes)==1:
            shapes[0].Shape.exportBrep(path)
        else:
            Part.makeCompound([o.Shape for o in shapes]).exportBrep(path)
        return stats

    if e==".amf":
        export_amf(doc,path)
        return stats

    if e in (".stl",".obj",".ply",".off"):
        export_mesh(doc,path)
        return stats

    if e==".dxf":
        if mesh_objects(doc):
            export_mesh_projection_dxf(doc,path)
            return stats
        try:
            import importDXF
            importDXF.export(doc_objects(doc),path)
        except Exception:
            Import.export(doc_objects(doc),path)
        return stats
    if e==".svg":
        if mesh_objects(doc):
            export_mesh_projection_svg(doc,path)
            return stats
        try:
            import importSVG
            importSVG.export(doc_objects(doc),path)
        except Exception:
            Import.export(doc_objects(doc),path)
        return stats
    if e in (".dae",".iv",".wrl",".vrml"):
        export_mesh_scene(doc,path,tolerance,"dae" if e==".dae" else "iv" if e==".iv" else "wrl")
        return stats

    Import.export(doc_objects(doc),path)
    return stats

def main():
    # FreeCADCmd keeps its own executable in argv[0] and passes the Python
    # worker path again as argv[1]. Plain Python does not. Normalize both
    # invocation styles before reading the conversion arguments.
    args=list(sys.argv[1:])
    if args and args[0].lower().endswith(".py"):
        args=args[1:]
    if len(args)<2:
        raise RuntimeError("Arguments manquants: entrée et sortie requises.")

    input_path=os.path.abspath(args[0])
    output_path=os.path.abspath(args[1])
    linear=float(args[2]) if len(args)>2 else 0.10
    angular=float(args[3]) if len(args)>3 else 0.523599
    max_facets=int(float(args[4])) if len(args)>4 else 250000

    if not os.path.isfile(input_path):
        raise RuntimeError("Fichier source introuvable: "+input_path)

    os.makedirs(os.path.dirname(output_path),exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)

    doc=import_file(input_path)
    try:
        if not doc.Objects:
            raise RuntimeError("FreeCAD n'a importé aucun objet depuis ce fichier.")

        input_mesh_stats=[]
        for mo in mesh_objects(doc):
            input_mesh_stats.append({"object":mo.Label,"facets":int(mo.Mesh.CountFacets)})

        stats=export_file(doc,output_path,linear,max_facets)

        if not os.path.isfile(output_path) or os.path.getsize(output_path)==0:
            raise RuntimeError("FreeCAD n'a produit aucun fichier de sortie.")

        result(
            True,
            input=os.path.basename(input_path),
            output=os.path.basename(output_path),
            bytes=os.path.getsize(output_path),
            freecad_version=".".join(map(str,App.Version()[:3])),
            objects=len(doc.Objects),
            input_meshes=input_mesh_stats,
            cad_conversion=stats,
            max_cad_facets=max_facets
        )
    finally:
        try:
            App.closeDocument(doc.Name)
        except Exception:
            pass

try:
    main()
except Exception as exc:
    traceback.print_exc()
    result(False,error=str(exc))
    sys.exit(2)
