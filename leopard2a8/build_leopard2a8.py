"""
Leopard 2A8 - prozedurales Echtzeit-Modell fuer Blender (>= 4.2, getestet mit 5.2)
inklusive Rig und Export fuer Unreal Engine 5.

WICHTIG: Alle Masse sind Naeherungswerte aus oeffentlich zugaenglichen Quellen
(Gesamtmasse, Laufwerksaufbau, Rohrlaenge L55 usw.). Es handelt sich NICHT um
Herstellerdaten und NICHT um ein CAD-genaues Modell. Siehe README.md.

Aufruf:
    blender -b -P leopard2a8/build_leopard2a8.py -- --out export
    # oder mit dem pip-Modul "bpy":
    python leopard2a8/build_leopard2a8.py --out export

Optionen:
    --out DIR        Ausgabeordner (Standard: ./export)
    --renders DIR    Ordner fuer Vorschaubilder (Standard: ./renders)
    --no-export      keine FBX-Dateien schreiben
    --no-render      keine Vorschaubilder rendern
    --samples N      Cycles-Samples fuer die Vorschau (Standard: 48)

Koordinaten: Blender +X = Fahrzeugfront, +Y = links, +Z = oben, 1 BU = 1 m.
Nullpunkt = Boden unter der Wannenmitte. Mit den Export-Einstellungen unten
zeigt das Fahrzeug in UE5 nach +X und hat korrekte Groesse (cm).
"""

import argparse
import json
import math
import os
import sys

import bpy  # muss vor bmesh importiert werden (pip-Modul)
import bmesh
from mathutils import Matrix, Vector

try:   # Bake-Modul liegt neben diesem Skript
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bake_l2a8
except (ImportError, NameError):
    bake_l2a8 = None

# =============================================================================
# Hauptmasse (Meter) - oeffentliche Naeherungswerte
# =============================================================================
HULL_X0, HULL_X1 = -3.86, 3.86      # Wannenlaenge ~7,7 m
LOWER_W = 1.02                      # halbe Breite Unterwanne (zwischen den Ketten)
UPPER_W = 1.72                      # halbe Breite Oberwanne (ueber den Ketten)
SPONSON_Z = 1.27                    # Unterkante Oberwanne
DECK_Z = 1.72                       # Hoehe Wannendach (Seitenkante)
BELLY_Z = 0.52                      # Bodenfreiheit inkl. Minenschutzplatte
GLACIS_X = 1.70                     # Beginn des oberen Glacis (flach bis zum Bug)
SKIRT_Z0, SKIRT_Z1 = 0.88, 1.40     # Kettenschuerzen Unter-/Oberkante

TRACK_Y = 1.39                      # Kettenmitte (Spurweite ~2,78 m)
TRACK_W = 0.635                     # Kettenbreite 635 mm
TRACK_T = 0.09                      # Kettendicke inkl. Gummipolster
TRACK_PITCH = 0.184                 # Kettengliedteilung (Naeherung)

RW_R = 0.35                         # Laufrollen-Radius (~700 mm Durchmesser)
RW_Z = TRACK_T + RW_R               # Laufrollen-Mittelpunkt
# 7 Laufrollen je Seite; Abstaende nach massstaeblichen Rissen (vorn etwas enger als hinten)
RW_X = [2.58, 1.76, 0.98, 0.18, -0.67, -1.51, -2.37]
SPROCKET = (-3.37, 0.86, 0.30)      # Triebrad hinten (x, z, Teilkreisradius)
IDLER = (3.33, 0.77, 0.30)          # Leitrad vorn
ROLLER_R = 0.10
ROLLER_Z = 1.00
ROLLER_X = [2.10, 0.70, -0.75, -2.20]  # 4 Stuetzrollen je Seite

TURRET_X = 0.15                     # Mitte Turmdrehkranz
TURRET_Z = 1.76                     # Unterkante Turm
ROOF_Z = 2.61                       # Turmdach
GUN_X, GUN_Z = 1.40, 2.09           # Schildzapfen (Hoehenrichtachse), Rohrachse ~2,09 m
CMD_HATCH = (0.05, -0.52, 0.36)     # Kommandantenluke (x, y, Radius), rechts
LDR_HATCH = (0.00, 0.50, 0.34)      # Ladeschuetzenluke, links
PERI_POS = (-0.55, -0.20)           # Kommandanten-Rundblickperiskop
RWS_POS = (-0.80, 0.80)             # FLW 200
TROPHY_POS = (-1.15, 1.47)          # Trophy-Werfer (x, |y|)
MUZZLE_X = HULL_X0 - 0.12 + 10.97   # Gesamtlaenge Rohr vorn ~10,97 m (inkl. Heckanbauten)
BORE_R = 0.06                       # 120 mm Kaliber

# =============================================================================
# Oberflaechentypen (werden als Vertex-Farbe "ColorID" fuer Substance/UE5
# und als Material-Masken gespeichert)
# =============================================================================
SURF = {"paint": 0, "metal": 1, "rubber": 2, "dark": 3, "glass": 4}
SURF_COLOR = {          # lineare RGB-Werte
    0: (0.055, 0.069, 0.040),   # Bronzegruen (Grundton)
    1: (0.090, 0.090, 0.085),   # blanker Stahl
    2: (0.015, 0.015, 0.015),   # Gummi
    3: (0.006, 0.006, 0.006),   # dunkle Innenflaechen / Gitter
    4: (0.010, 0.020, 0.030),   # Optik-Glas
}
SURF_ROUGH = {0: 0.62, 1: 0.42, 2: 0.90, 3: 0.80, 4: 0.05}
SURF_METAL = {0: 0.0, 1: 0.85, 2: 0.0, 3: 0.2, 4: 0.0}

# Material = Texture-Set (je Gruppe ein Material, Glas separat)
GROUP_MATERIAL = {
    "hull": "MI_L2A8_Hull",
    "turret": "MI_L2A8_Turret",
    "gun": "MI_L2A8_Gun",
    "wheels": "MI_L2A8_RunningGear",
    "track": "MI_L2A8_Track",
    "track_belt": "MI_L2A8_TrackBelt",
}
GLASS_MATERIAL = "MI_L2A8_Optics"


# =============================================================================
# Geometrie-Baukasten
# =============================================================================
def _rot_from_to(a, b):
    return Vector(a).rotation_difference(Vector(b)).to_matrix().to_4x4()


def _euler_deg(rot):
    from mathutils import Euler
    return Euler([math.radians(r) for r in rot], "XYZ").to_matrix().to_4x4()


class Part:
    """Sammelt Geometrie (Weltkoordinaten) fuer ein Objekt, das an einem Bone haengt."""

    def __init__(self, name, bone, group, pivot=(0, 0, 0), bevel=0.012, weld=False):
        self.name, self.bone, self.group = name, bone, group
        self.pivot = Vector(pivot)
        self.bevel = bevel
        self.weld = weld            # scharfe Kanten als Schweissnaehte fuer die Normal Map merken
        self.bm = bmesh.new()
        self.surf = self.bm.faces.layers.int.new("surf")

    # -- intern -------------------------------------------------------------
    def _tag(self, verts, surf):
        sid = SURF[surf]
        for f in {f for v in verts if v.is_valid for f in v.link_faces}:
            f[self.surf] = sid

    # -- Primitive ----------------------------------------------------------
    def box(self, center, size, surf="paint", rot=None):
        m = Matrix.Translation(Vector(center))
        if rot is not None:
            m = m @ (rot if isinstance(rot, Matrix) else _euler_deg(rot))
        m = m @ Matrix.Diagonal((*size, 1.0))
        self._tag(bmesh.ops.create_cube(self.bm, size=1.0, matrix=m)["verts"], surf)

    def box_mm(self, lo, hi, surf="paint"):
        lo, hi = Vector(lo), Vector(hi)
        self.box((lo + hi) / 2, hi - lo, surf)

    def beam(self, p0, p1, w, h, surf="paint"):
        """Quader entlang p0->p1 (Breite w quer, Hoehe h)."""
        p0, p1 = Vector(p0), Vector(p1)
        d = p1 - p0
        m = Matrix.Translation((p0 + p1) / 2) @ _rot_from_to((1, 0, 0), d)
        m = m @ Matrix.Diagonal((d.length, w, h, 1.0))
        self._tag(bmesh.ops.create_cube(self.bm, size=1.0, matrix=m)["verts"], surf)

    def cyl(self, p0, p1, r, r2=None, seg=24, surf="paint", caps=True):
        p0, p1 = Vector(p0), Vector(p1)
        d = p1 - p0
        m = Matrix.Translation((p0 + p1) / 2) @ _rot_from_to((0, 0, 1), d)
        res = bmesh.ops.create_cone(
            self.bm, cap_ends=caps, cap_tris=False, segments=seg,
            radius1=r, radius2=r if r2 is None else r2, depth=d.length, matrix=m)
        self._tag(res["verts"], surf)

    def hull(self, pts, surf="paint"):
        """Konvexe Huelle einer Punktwolke, koplanare Dreiecke werden zusammengefasst."""
        bm = self.bm
        vs = [bm.verts.new(Vector(p)) for p in pts]
        res = bmesh.ops.convex_hull(bm, input=vs, use_existing_faces=False)
        junk = {g for g in res["geom_interior"] + res["geom_unused"]
                if isinstance(g, bmesh.types.BMVert)}
        if junk:
            bmesh.ops.delete(bm, geom=list(junk), context="VERTS")
        vs = [v for v in vs if v.is_valid]
        edges = list({e for v in vs for e in v.link_edges})
        bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(0.5), verts=vs, edges=edges)
        self._tag([v for v in vs if v.is_valid], surf)

    def prism(self, poly, plane, a0, a1, surf="paint"):
        """2D-Polygon in einer Ebene ('XZ','XY','YZ') entlang der dritten Achse extrudieren."""
        def P(p, a):
            if plane == "XZ":
                return Vector((p[0], a, p[1]))
            if plane == "XY":
                return Vector((p[0], p[1], a))
            return Vector((a, p[0], p[1]))
        bm = self.bm
        b = [bm.verts.new(P(p, a0)) for p in poly]
        t = [bm.verts.new(P(p, a1)) for p in poly]
        bm.faces.new(b)
        bm.faces.new(list(reversed(t)))
        n = len(poly)
        for i in range(n):
            j = (i + 1) % n
            bm.faces.new((b[i], b[j], t[j], t[i]))
        self._tag(b + t, surf)

    def lathe(self, center, axis, prof, seg=32, surf="paint", closed=False):
        """Rotationskoerper: prof = [(axial, radius), ...]; surf = str oder Liste je Segment.
        closed=True verbindet das letzte Profil wieder mit dem ersten (z. B. Torus)."""
        bm = self.bm
        center, axis = Vector(center), Vector(axis).normalized()
        u = axis.orthogonal().normalized()
        v = axis.cross(u)
        surfs = surf if isinstance(surf, (list, tuple)) else [surf] * len(prof)
        rings = []
        for a, r in prof:
            c = center + axis * a
            if r <= 1e-6:
                rings.append([bm.verts.new(c)])
            else:
                rings.append([bm.verts.new(c + (u * math.cos(2 * math.pi * i / seg)
                                                 + v * math.sin(2 * math.pi * i / seg)) * r)
                              for i in range(seg)])
        for k in range(len(rings) if closed else len(rings) - 1):
            A, B = rings[k], rings[(k + 1) % len(rings)]
            if len(A) == 1 and len(B) == 1:
                continue
            sid = SURF[surfs[k]]
            for i in range(seg):
                j = (i + 1) % seg
                if len(A) == 1:
                    f = bm.faces.new((A[0], B[j], B[i]))
                elif len(B) == 1:
                    f = bm.faces.new((A[i], A[j], B[0]))
                else:
                    f = bm.faces.new((A[i], A[j], B[j], B[i]))
                f[self.surf] = sid
        if closed:
            return
        for ring, s in ((rings[0], surfs[0]), (rings[-1], surfs[len(prof) - 2])):
            if len(ring) > 1:
                bm.faces.new(ring)[self.surf] = SURF[s]

    def sphere(self, c, r, seg=8, surf="metal"):
        prof = [(r * math.cos(a), r * math.sin(a)) for a in
                [math.pi - k * math.pi / (seg // 2) for k in range(seg // 2 + 1)]]
        self.lathe(c, (0, 0, 1), [(a, max(rr, 0.0)) for a, rr in prof], seg=seg, surf=surf)

    def tube(self, pts, r, seg=8, surf="metal"):
        """Rohr/Seil entlang eines Polygonzugs mit Kugelgelenken."""
        pts = [Vector(p) for p in pts]
        for a, b in zip(pts, pts[1:]):
            self.cyl(a, b, r, seg=seg, surf=surf)
        for p in pts[1:-1]:
            self.sphere(p, r, seg=seg, surf=surf)

    def handle(self, p0, p1, normal, h=0.07, r=0.012, surf="paint"):
        """Haltegriff: zwei Fuesse von p0/p1 entlang normal, Querstange dazwischen."""
        n = Vector(normal).normalized()
        p0, p1 = Vector(p0), Vector(p1)
        self.tube([p0, p0 + n * h, p1 + n * h, p1], r, surf=surf)

    def eye(self, c, normal, axis, R=0.04, t=0.012, surf="paint"):
        """Zurr-/Hebeoese: Sockel + stehender Ring (Ringebene enthaelt normal)."""
        n, ax = Vector(normal).normalized(), Vector(axis).normalized()
        c = Vector(c)
        side = n.cross(ax).normalized()
        m = Matrix((ax, side, n)).transposed().to_4x4()
        m.translation = c + n * 0.015
        self._tag(bmesh.ops.create_cube(self.bm, size=1.0,
                                        matrix=m @ Matrix.Diagonal((2.2 * t, 2.6 * R, 0.03, 1)))["verts"], surf)
        prof = [(t * math.sin(a), R + t * math.cos(a)) for a in [k * math.pi / 3 for k in range(6)]]
        self.lathe(c + n * (0.03 + R), ax, prof, seg=12, surf=surf, closed=True)

    def loft(self, sections, surf="paint", cap=True):
        bm = self.bm
        rings = [[bm.verts.new(Vector(p)) for p in sec] for sec in sections]
        n = len(rings[0])
        for A, B in zip(rings, rings[1:]):
            for i in range(n):
                j = (i + 1) % n
                bm.faces.new((A[i], A[j], B[j], B[i]))
        if cap:
            bm.faces.new(rings[0])
            bm.faces.new(list(reversed(rings[-1])))
        self._tag([v for r in rings for v in r], surf)

    def shift(self, dz):
        bmesh.ops.translate(self.bm, verts=self.bm.verts[:], vec=(0, 0, dz))
        self.pivot.z += dz

    def bolts(self, pts, normal, r=0.018, h=0.015, surf="metal", seg=6):
        n = Vector(normal).normalized()
        for p in pts:
            p = Vector(p)
            self.cyl(p, p + n * h, r, r * 0.8, seg=seg, surf=surf)


# =============================================================================
# Szene / Materialien
# =============================================================================
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    s = bpy.context.scene
    s.unit_settings.system = "METRIC"
    s.unit_settings.scale_length = 1.0
    s.unit_settings.length_unit = "METERS"


def get_collection(name, parent=None):
    c = bpy.data.collections.get(name)
    if c is None:
        c = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(c)
    return c


def _new_nodetree(mat):
    if mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (600, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (300, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return nt, bsdf


def make_materials():
    mats = {}
    for group, name in GROUP_MATERIAL.items():
        mat = bpy.data.materials.new(name)
        nt, bsdf = _new_nodetree(mat)
        # Default-Werte landen im FBX (UE5 legt daraus Parameter an)
        bsdf.inputs["Base Color"].default_value = (*SURF_COLOR[0], 1.0)
        bsdf.inputs["Roughness"].default_value = SURF_ROUGH[0]
        bsdf.inputs["Metallic"].default_value = 0.0
        # Vorschau in Blender: ColorID + 3-Ton-Tarnung auf lackierten Flaechen
        cid = nt.nodes.new("ShaderNodeVertexColor")
        cid.layer_name = "ColorID"
        cid.location = (-700, 200)
        a_paint = nt.nodes.new("ShaderNodeAttribute")
        a_paint.attribute_name = "paint_mask"
        a_paint.location = (-700, 0)
        a_rough = nt.nodes.new("ShaderNodeAttribute")
        a_rough.attribute_name = "roughness"
        a_rough.location = (-200, -250)
        a_metal = nt.nodes.new("ShaderNodeAttribute")
        a_metal.attribute_name = "metallic"
        a_metal.location = (-200, -450)
        geo = nt.nodes.new("ShaderNodeNewGeometry")
        geo.location = (-1100, 400)
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.location = (-900, 400)
        noise.inputs["Scale"].default_value = 0.42
        noise.inputs["Detail"].default_value = 1.5
        noise.inputs["Roughness"].default_value = 0.45
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.location = (-700, 450)
        ramp.color_ramp.interpolation = "CONSTANT"
        el = ramp.color_ramp.elements
        el[0].position, el[0].color = 0.0, (0.013, 0.013, 0.011, 1)     # Schwarz
        el[1].position, el[1].color = 0.40, (0.055, 0.069, 0.040, 1)    # Gruen
        e = el.new(0.62)
        e.color = (0.089, 0.051, 0.027, 1)                              # Braun
        e = el.new(0.40)
        e.position, e.color = 0.40, (0.055, 0.069, 0.040, 1)
        el[0].position = 0.36
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.location = (-300, 250)
        nt.links.new(geo.outputs["Position"], noise.inputs["Vector"])
        nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
        nt.links.new(a_paint.outputs["Fac"], mix.inputs["Factor"])
        nt.links.new(cid.outputs["Color"], mix.inputs["A"])
        nt.links.new(ramp.outputs["Color"], mix.inputs["B"])
        nt.links.new(mix.outputs["Result"], bsdf.inputs["Base Color"])
        nt.links.new(a_rough.outputs["Fac"], bsdf.inputs["Roughness"])
        nt.links.new(a_metal.outputs["Fac"], bsdf.inputs["Metallic"])
        mats[group] = mat
    glass = bpy.data.materials.new(GLASS_MATERIAL)
    _, bsdf = _new_nodetree(glass)
    bsdf.inputs["Base Color"].default_value = (*SURF_COLOR[4], 1.0)
    bsdf.inputs["Roughness"].default_value = 0.05
    bsdf.inputs["Metallic"].default_value = 0.3
    mats["glass"] = glass
    return mats


# =============================================================================
# Teil -> Blender-Objekt (Bevel, Normalen, Attribute, Material)
# =============================================================================
def weld_edges(bm, min_len=0.12, angle=40):
    """Scharfe Kanten (Plattenstoesse) als [p1, p2, normal, konvex] fuer gebackene Schweissnaehte."""
    out = []
    for e in bm.edges:
        if len(e.link_faces) != 2 or e.calc_length() < min_len:
            continue
        if e.calc_face_angle(0.0) < math.radians(angle):
            continue
        n = (e.link_faces[0].normal + e.link_faces[1].normal).normalized()
        a, b = e.verts[0].co, e.verts[1].co
        out.extend([*a, *b, *n, 1.0 if e.is_convex else 0.0])
    return out


def finalize(part, coll, mats):
    bm = part.bm
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    welds = weld_edges(bm) if part.weld else []
    bmesh.ops.translate(bm, verts=bm.verts[:], vec=-part.pivot)
    me = bpy.data.meshes.new(part.name)
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new(part.name, me)
    coll.objects.link(obj)
    obj.location = part.pivot
    obj["bone"] = part.bone
    obj["group"] = part.group
    if welds:
        obj["weld_edges"] = welds

    surf = [0] * len(me.polygons)
    me.attributes["surf"].data.foreach_get("value", surf)
    has_glass = 4 in surf
    me.materials.append(mats[part.group])
    if has_glass:
        me.materials.append(mats["glass"])
    me.polygons.foreach_set("material_index", [1 if (s == 4 and has_glass) else 0 for s in surf])
    for attr, table in (("paint_mask", {0: 1.0}), ("roughness", SURF_ROUGH), ("metallic", SURF_METAL)):
        a = me.attributes.new(attr, "FLOAT", "FACE")
        a.data.foreach_set("value", [table.get(s, 0.0) for s in surf])
    me.shade_smooth()
    me.set_sharp_from_angle(angle=math.radians(30))

    if part.bevel:
        mod = obj.modifiers.new("Bevel", "BEVEL")
        mod.width = part.bevel
        mod.segments = 2
        mod.limit_method = "ANGLE"
        mod.angle_limit = math.radians(30)
        mod.harden_normals = True
        mod.use_clamp_overlap = True
        mod.miter_outer = "MITER_ARC"
        with bpy.context.temp_override(object=obj, active_object=obj):
            bpy.ops.object.modifier_apply(modifier=mod.name)

    # ColorID nach dem Bevel setzen (Flaechenattribute werden vom Bevel uebernommen)
    surf = [0] * len(me.polygons)
    me.attributes["surf"].data.foreach_get("value", surf)
    col = me.color_attributes.new("ColorID", "FLOAT_COLOR", "CORNER")
    cols = []
    for p in me.polygons:
        c = SURF_COLOR[surf[p.index]]
        cols.extend((*c, 1.0) * p.loop_total)
    col.data.foreach_set("color", cols)
    me.color_attributes.active_color = col
    return obj


# =============================================================================
# Wanne
# =============================================================================
def glacis_z(x):
    """Hoehe des oberen Glacis an Position x."""
    return DECK_Z + (x - GLACIS_X) / (3.62 - GLACIS_X) * (1.40 - DECK_Z)


def build_hull():
    parts = []
    h = Part("Hull_Body", "root", "hull", bevel=0.02, weld=True)
    # Unterwanne (zwischen den Ketten)
    h.prism([(-3.80, SPONSON_Z + 0.02), (-3.80, 0.80), (-3.52, 0.58), (3.02, 0.56),
             (3.70, 1.00), (3.70, SPONSON_Z + 0.02)], "XZ", -LOWER_W, LOWER_W)
    # Oberwanne mit flachem oberem Glacis
    h.prism([(HULL_X0, SPONSON_Z), (HULL_X0, DECK_Z - 0.10), (-3.76, DECK_Z - 0.01), (GLACIS_X, DECK_Z),
             (3.62, 1.40), (HULL_X1, 1.34), (HULL_X1, SPONSON_Z)], "XZ", -UPPER_W, UPPER_W)
    parts.append(h)

    d = Part("Hull_Armor", "root", "hull", bevel=0.015, weld=True)
    # Frontmodul (2A7/2A8-Zusatzpanzerung Bug)
    d.hull([(3.52, y, z) for y in (-1.60, 1.60) for z in (0.92, 1.38)] +
           [(3.92, y, 1.02) for y in (-1.56, 1.56)] + [(3.97, y, 1.30) for y in (-1.56, 1.56)] +
           [(3.72, y, 1.39) for y in (-1.58, 1.58)])
    # Minenschutzplatte Boden
    d.box_mm((-3.30, -1.00, BELLY_Z), (2.95, 1.00, 0.565))
    d.hull([(2.95, y, z) for y in (-1.0, 1.0) for z in (BELLY_Z, 0.565)] +
           [(3.30, y, 0.80) for y in (-0.95, 0.95)])
    # Turmdrehkranz-Schutzring
    d.cyl((TURRET_X, 0, DECK_Z - 0.01), (TURRET_X, 0, TURRET_Z - 0.005), 1.15, seg=64)
    # Seitliche Kettenabdeckung vorn (Kotfluegel) mit Scheinwerfern
    for s in (-1, 1):
        d.hull([(3.50, s * 1.06, 1.38), (3.50, s * 1.80, 1.38), (3.50, s * 1.06, 1.32), (3.50, s * 1.80, 1.32),
                (3.90, s * 1.10, 1.24), (3.90, s * 1.80, 1.24), (3.90, s * 1.10, 1.19), (3.90, s * 1.80, 1.19)])
        # Scheinwerfergehaeuse + Schutzbuegel
        d.box((3.55, s * 1.52, 1.47), (0.16, 0.24, 0.14))
        d.box((3.636, s * 1.52, 1.47), (0.012, 0.18, 0.09), surf="glass")
        d.beam((3.48, s * 1.36, 1.40), (3.70, s * 1.36, 1.56), 0.03, 0.03)
        d.beam((3.48, s * 1.68, 1.40), (3.70, s * 1.68, 1.56), 0.03, 0.03)
        # Abschleppoesen vorn
        d.box((3.94, s * 0.85, 1.12), (0.10, 0.10, 0.16), surf="metal")
        d.cyl((3.96, s * 0.80, 1.12), (3.96, s * 0.90, 1.12), 0.04, seg=12, surf="metal")
    # Fahrerluke (rechts vorn, im Glacis) + Winkelspiegel
    gz = glacis_z(2.05)
    d.cyl((2.05, -0.68, gz - 0.04), (2.05, -0.68, gz + 0.04), 0.36, seg=32)
    d.cyl((2.05, -0.68, gz + 0.04), (2.05, -0.68, gz + 0.10), 0.31, seg=32)
    gz = glacis_z(2.42)
    for dy in (-0.22, 0.0, 0.22):
        d.box((2.42, -0.68 + dy, gz + 0.03), (0.10, 0.17, 0.07), rot=(0, -10, 0))
        d.box((2.475, -0.68 + dy, gz + 0.03), (0.008, 0.13, 0.04), surf="glass", rot=(0, -10, 0))
    # Werkzeug-/Staukaesten auf den Kettenabdeckungen hinten
    for s in (-1, 1):
        for x0, x1 in ((-1.05, -2.15), (-2.30, -3.55)):
            d.box_mm((x1, min(s * 1.42, s * 1.70), DECK_Z), (x0, max(s * 1.42, s * 1.70), DECK_Z + 0.18))
        # Lueftungsgitter in der Wannenseite (unter dem Turmheck)
        for x0, x1 in ((-0.95, -0.55), (-1.45, -1.05)):
            yo = s * (UPPER_W + 0.01)
            d.box_mm((x0, min(yo, s * (UPPER_W - 0.02)), 1.44), (x1, max(yo, s * (UPPER_W - 0.02)), 1.66), "dark")
            for i in range(5):
                d.box(((x0 + x1) / 2, s * (UPPER_W + 0.02), 1.47 + i * 0.04), (x1 - x0, 0.02, 0.012), "metal",
                      rot=(s * 35, 0, 0))
    # Mittlerer Aufbau auf dem Bugmodul
    d.hull([(3.52, y, z) for y in (-0.55, 0.55) for z in (1.38, 1.50)] +
           [(3.80, y, z) for y in (-0.52, 0.52) for z in (1.36, 1.44)])
    parts.append(d)

    # Motordeck-Gitter (Ansaug- und Kuehlerluftgitter)
    g = Part("Hull_EngineDeck", "root", "hull", bevel=0.004)

    def grille(x0, x1, y0, y1, slat=0.065):
        g.box_mm((x0, y0, DECK_Z - 0.03), (x1, y1, DECK_Z + 0.005), "dark")
        t = 0.035
        g.box_mm((x0 - t, y0 - t, DECK_Z), (x1 + t, y0, DECK_Z + 0.035))
        g.box_mm((x0 - t, y1, DECK_Z), (x1 + t, y1 + t, DECK_Z + 0.035))
        g.box_mm((x0 - t, y0, DECK_Z), (x0, y1, DECK_Z + 0.035))
        g.box_mm((x1, y0, DECK_Z), (x1 + t, y1, DECK_Z + 0.035))
        n = int((x1 - x0) / slat)
        for i in range(1, n):
            x = x0 + i * (x1 - x0) / n
            g.box((x, (y0 + y1) / 2, DECK_Z + 0.012), (0.012, y1 - y0, 0.03), "metal", rot=(0, 25, 0))

    for s in (-1, 1):
        y0, y1 = sorted((s * 0.18, s * 1.30))
        grille(-2.15, -1.10, y0, y1)
        y0, y1 = sorted((s * 0.12, s * 1.36))
        grille(-3.62, -2.35, y0, y1)
    # Wartungsklappen zwischen den Gittern
    g.box_mm((-3.62, -0.08, DECK_Z), (-1.10, 0.08, DECK_Z + 0.02))
    parts.append(g)

    # Heck
    r = Part("Hull_Rear", "root", "hull", bevel=0.008)
    for s in (-1, 1):
        y0, y1 = sorted((s * 0.92, s * 1.62))
        # Abgasgitter (schraeg) am oberen Heck
        r.box_mm((HULL_X0 - 0.03, y0, 1.30), (HULL_X0 + 0.005, y1, 1.58), "dark")
        for i in range(6):
            z = 1.32 + i * 0.047
            r.box(((HULL_X0 - 0.04), (y0 + y1) / 2, z), (0.03, y1 - y0, 0.012), "metal", rot=(0, -30, 0))
        # Rueckleuchten
        r.box((HULL_X0 - 0.04, s * 1.58, 1.20), (0.08, 0.18, 0.10))
        r.box((HULL_X0 - 0.081, s * 1.55, 1.20), (0.004, 0.08, 0.05), "glass")
        # Abschlepphaken
        r.box((HULL_X0 - 0.06, s * 0.70, 0.98), (0.12, 0.10, 0.16), "metal")
        r.cyl((HULL_X0 - 0.12, s * 0.65, 0.98), (HULL_X0 - 0.12, s * 0.75, 0.98), 0.05, seg=12, surf="metal")
        # Kettenschuerzen-Schmutzfaenger hinten
        y0, y1 = sorted((s * 1.06, s * 1.78))
        r.box_mm((HULL_X0 - 0.04, y0, 0.98), (HULL_X0 + 0.04, y1, SPONSON_Z), "rubber")
    # Anhaengerkupplung, Infanterietelefon (Kasten rechts)
    r.box((HULL_X0 - 0.07, 0, 0.92), (0.14, 0.22, 0.18), "metal")
    r.box((HULL_X0 - 0.07, -1.20, 1.00), (0.14, 0.22, 0.26))
    parts.append(r)

    # Kettenschuerzen (vorne schwer gepanzert, hinten leicht)
    for s, side in ((1, "L"), (-1, "R")):
        k = Part(f"Hull_Skirts_{side}", "root", "hull", bevel=0.01)
        xs = [3.62, 2.76, 1.92, 1.06]
        for i in range(3):
            x1, x0 = xs[i] - 0.008, xs[i + 1] + 0.008
            yi, yo = s * 1.765, s * 1.885
            # Unterkante etwa auf Hoehe der Laufrollen-Oberkante (Laufrollen bleiben sichtbar)
            pts = [(x0, y, z) for y in (yi, yo) for z in (SKIRT_Z0, SKIRT_Z1)]
            if i == 0:
                pts += [(x1, yi, SKIRT_Z0 + 0.20), (x1 - 0.14, yo, SKIRT_Z0 + 0.06), (x1, yi, SKIRT_Z1),
                        (x1, yo, SKIRT_Z1), (x1 - 0.22, yi, SKIRT_Z0), (x1 - 0.24, yo, SKIRT_Z0 + 0.02)]
            else:
                pts += [(x1, y, z) for y in (yi, yo) for z in (SKIRT_Z0, SKIRT_Z1)]
            k.hull(pts)
            bolt_x = [x0 + (x1 - x0) * f for f in (0.15, 0.5, 0.85)]
            k.bolts([(bx, yo, z) for bx in bolt_x for z in (SKIRT_Z0 + 0.09, SKIRT_Z1 - 0.09)], (0, s, 0), r=0.022, h=0.012)
        xs = [1.06, -0.12, -1.30, -2.48, -3.66]
        for i in range(4):
            x1, x0 = xs[i] - 0.008, xs[i + 1] + 0.008
            y0, y1 = sorted((s * 1.765, s * 1.80))
            k.box_mm((x0, y0, SKIRT_Z0), (x1, y1, SKIRT_Z1))
        # Schuerzen-Halteleiste an der Oberwanne
        y0, y1 = sorted((s * 1.72, s * 1.78))
        k.box_mm((-3.70, y0, SKIRT_Z1 - 0.06), (3.62, y1, SKIRT_Z1 + 0.02))
        # Scharniere an der Schuerzen-Oberkante
        for x in (3.40, 2.55, 1.70, 0.90, -0.30, -1.48, -2.66, -3.50):
            k.cyl((x - 0.07, s * 1.80, SKIRT_Z1 - 0.03), (x + 0.07, s * 1.80, SKIRT_Z1 - 0.03), 0.022, seg=10,
                  surf="metal")
        # V-foermige Daempfer-/Anschlagkonsolen unter der Schuerze
        for x in (RW_X[0], RW_X[1], RW_X[5], RW_X[6]):
            for dx in (-0.17, 0.17):
                k.beam((x + dx, s * 1.74, SKIRT_Z0 + 0.02), (x, s * 1.74, SKIRT_Z0 - 0.18), 0.04, 0.05)
            k.box((x, s * 1.74, SKIRT_Z0 - 0.18), (0.08, 0.05, 0.05), "metal")
        parts.append(k)

    # Anbauteile: Griffe, Oesen, Abschleppseile, Schmutzfaenger
    f = Part("Hull_Fittings", "root", "hull", bevel=0.0)
    for s in (-1, 1):
        # Zurr-/Hebeoesen an den Wannenecken
        f.eye((3.30, s * 1.30, glacis_z(3.30)), (0.2, 0, 1), (1, 0, 0))
        f.eye((-3.60, s * 1.45, DECK_Z), (0, 0, 1), (1, 0, 0))
        # Haltegriffe: Wannenseite vorn, Heckplatte oben
        f.handle((2.95, s * UPPER_W, 1.50), (3.25, s * UPPER_W, 1.50), (0, s, 0))
        f.handle((HULL_X0, s * 0.30, DECK_Z - 0.16), (HULL_X0, s * 0.62, DECK_Z - 0.16), (-1, 0, 0))
        # Griffe auf dem Motordeck (Wartungsklappen), flach liegend
        f.handle((-1.30, s * 0.40, DECK_Z + 0.035), (-1.30, s * 0.70, DECK_Z + 0.035), (0, 0, 1), h=0.05)
        f.handle((-2.55, s * 0.40, DECK_Z + 0.035), (-2.55, s * 0.70, DECK_Z + 0.035), (0, 0, 1), h=0.05)
        # Schmutzfaenger vorn unter dem Kotfluegel
        y0, y1 = sorted((s * 1.10, s * 1.78))
        f.box_mm((3.86, y0, 0.96), (3.89, y1, 1.20), "rubber")
        # Feuerloescher-Zuggriff an der Wannenseite
        f.eye((2.70, s * UPPER_W, 1.42), (0, s, 0), (1, 0, 0), R=0.03, t=0.008)
    # Abschleppseile ueber Kreuz an der Heckplatte, mit Seilkauschen
    for s, dx in ((1, 0.035), (-1, 0.068)):
        a_ = Vector((HULL_X0 - dx, s * 1.30, 1.26))
        b_ = Vector((HULL_X0 - dx, -s * 0.95, 1.10))
        mid1 = a_.lerp(b_, 0.33) + Vector((-0.01, 0, -0.02))
        mid2 = a_.lerp(b_, 0.66) + Vector((-0.01, 0, -0.02))
        f.tube([a_, mid1, mid2, b_], 0.016, surf="metal")
        for p_, q_ in ((a_, b_), (b_, a_)):     # Kauschen flach an der Heckplatte, in Seilrichtung
            f.eye(p_, (p_ - q_).normalized(), (1, 0, 0), R=0.035, t=0.012, surf="metal")
    parts.append(f)
    return parts


# =============================================================================
# Laufwerk
# =============================================================================
def build_running_gear():
    parts = []
    half = [(0.055, 0.0), (0.055, 0.10), (0.085, 0.13), (0.085, 0.265), (0.06, 0.29),
            (0.06, 0.335), (0.075, RW_R), (0.285, RW_R), (0.30, 0.335), (0.30, 0.29),
            (0.27, 0.265), (0.27, 0.14), (0.31, 0.11), (0.31, 0.0)]
    hs = ["paint"] * 4 + ["rubber"] * 5 + ["paint"] * 4
    inner = [(-a, r) for a, r in reversed(half)]
    for s, side in ((1, "L"), (-1, "R")):
        yc = s * TRACK_Y
        # Laufrollen (Doppelrollen mit Gummibandage)
        for i, x in enumerate(RW_X, 1):
            c = Vector((x, yc, RW_Z))
            p = Part(f"RoadWheel_{side}_{i:02d}", f"wheel_{side.lower()}_{i:02d}", "wheels", pivot=c, bevel=0)
            p.lathe(c, (0, s, 0), half, seg=32, surf=hs)
            p.lathe(c, (0, s, 0), inner, seg=32, surf=list(reversed(hs)))
            p.cyl(c + Vector((0, s * 0.31, 0)), c + Vector((0, s * 0.35, 0)), 0.085, 0.07, seg=24, surf="metal")
            p.bolts([c + Vector((math.cos(a) * 0.19, s * 0.27, math.sin(a) * 0.19))
                     for a in [k * math.pi / 5 for k in range(10)]], (0, s, 0), r=0.016, h=0.012)
            parts.append(p)
        # Leitrad (vorn)
        x, z, r = IDLER
        c = Vector((x, yc, z))
        p = Part(f"Idler_{side}", f"idler_{side.lower()}", "wheels", pivot=c, bevel=0)
        prof = [(0.055, 0.0), (0.055, r - 0.02), (0.07, r), (0.29, r), (0.30, r - 0.02),
                (0.30, r - 0.05), (0.26, r - 0.07), (0.26, 0.13), (0.31, 0.10), (0.31, 0.0)]
        ps = ["paint", "metal", "metal", "metal", "metal", "paint", "paint", "paint", "paint"]
        p.lathe(c, (0, s, 0), prof, seg=32, surf=ps)
        p.lathe(c, (0, s, 0), [(-a, rr) for a, rr in reversed(prof)], seg=32, surf=list(reversed(ps)))
        for k in range(6):
            a = k * math.pi / 3
            p.box(c + Vector((math.cos(a) * 0.19, s * 0.27, math.sin(a) * 0.19)), (0.06, 0.03, 0.06), "paint")
        parts.append(p)
        # Triebrad (hinten) mit zwei Zahnkraenzen
        x, z, r = SPROCKET
        c = Vector((x, yc, z))
        p = Part(f"Sprocket_{side}", f"sprocket_{side.lower()}", "wheels", pivot=c, bevel=0)
        p.lathe(c, (0, s, 0), [(-0.33, 0.0), (-0.33, 0.20), (-0.25, 0.27), (0.25, 0.27),
                              (0.33, 0.20), (0.36, 0.12), (0.36, 0.0)], seg=32)
        teeth, rr, rt = 12, r - 0.03, r + 0.05
        gear = []
        for k in range(teeth):
            a0 = 2 * math.pi * k / teeth
            for da, rad in ((-0.17, rr), (-0.08, rt), (0.08, rt), (0.17, rr)):
                gear.append((x + math.cos(a0 + da) * rad, z + math.sin(a0 + da) * rad))
        for a0, a1 in ((0.25, 0.31), (-0.31, -0.25)):
            p.prism(gear, "XZ", yc + s * a0, yc + s * a1, "metal")
        p.bolts([c + Vector((math.cos(a) * 0.15, s * 0.36, math.sin(a) * 0.15))
                 for a in [k * math.pi / 4 for k in range(8)]], (0, s, 0), r=0.018, h=0.012)
        parts.append(p)
        # Stuetzrollen
        for i, x in enumerate(ROLLER_X, 1):
            c = Vector((x, yc, ROLLER_Z))
            p = Part(f"ReturnRoller_{side}_{i:02d}", f"roller_{side.lower()}_{i:02d}", "wheels", pivot=c, bevel=0)
            prof = [(0.04, 0.0), (0.04, ROLLER_R - 0.015), (0.055, ROLLER_R), (0.27, ROLLER_R),
                    (0.29, ROLLER_R - 0.02), (0.29, 0.0)]
            ps = ["paint", "rubber", "rubber", "rubber", "paint"]
            p.lathe(c, (0, s, 0), prof, seg=20, surf=ps)
            p.lathe(c, (0, s, 0), [(-a, rr) for a, rr in reversed(prof)], seg=20, surf=list(reversed(ps)))
            p.cyl(c + Vector((0, -s * 0.29, 0)), Vector((x, s * (LOWER_W - 0.01), ROLLER_Z)), 0.045, seg=12, surf="metal")
            parts.append(p)

        # Statische Teile: Schwingarme, Anschlaege, Achsantrieb, Leitradkurbel
        a = Part(f"Suspension_{side}", "root", "wheels", bevel=0.008)
        hub_y = s * (TRACK_Y - 0.33)
        for x in RW_X:
            hub = Vector((x, hub_y, RW_Z))
            piv = Vector((x + 0.46, s * (LOWER_W + 0.02), 0.66))
            a.beam(hub, piv, 0.10, 0.12)
            a.cyl(hub + Vector((0, s * 0.03, 0)), Vector((x, s * (LOWER_W - 0.01), RW_Z)), 0.085, seg=20, surf="metal")
            a.cyl(piv + Vector((0, s * 0.03, 0)), Vector((piv.x, s * (LOWER_W - 0.01), piv.z)), 0.10, seg=20)
            a.box((x + 0.05, s * (LOWER_W + 0.06), 0.95), (0.16, 0.12, 0.10))      # Anschlag
        x, z, _ = SPROCKET
        a.cyl((x, s * (LOWER_W - 0.02), z), (x, s * (TRACK_Y - 0.34), z), 0.24, seg=32)
        x, z, _ = IDLER
        a.beam((x, s * (TRACK_Y - 0.34), z), (x - 0.38, s * (LOWER_W + 0.04), 0.90), 0.12, 0.14)
        a.cyl((x, s * (LOWER_W - 0.01), z), (x, s * (TRACK_Y - 0.33), z), 0.09, seg=20, surf="metal")
        parts.append(a)
    return parts


def track_path():
    """Mittellinie der Kette als konvexe Huelle aller umlaufenen Raeder (x,z), gegen den Uhrzeigersinn."""
    t2 = TRACK_T / 2
    circles = [(SPROCKET[0], SPROCKET[1], SPROCKET[2] + t2), (IDLER[0], IDLER[1], IDLER[2] + t2)]
    circles += [(x, RW_Z, RW_R + t2) for x in RW_X]
    circles += [(x, ROLLER_Z, ROLLER_R + t2) for x in ROLLER_X]
    pts = []
    for cx, cz, r in circles:
        for i in range(720):
            a = 2 * math.pi * i / 720
            pts.append((cx + r * math.cos(a), cz + r * math.sin(a)))
    pts = sorted(set(pts))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    poly = lower[:-1] + upper[:-1]
    # Bogenlaenge
    seg = [math.dist(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]
    return poly, seg, sum(seg)


def sample_path(poly, seg, total, n):
    """n gleichmaessig verteilte Punkte (Position, Tangente) entlang des geschlossenen Pfads."""
    out = []
    i, acc = 0, 0.0
    for k in range(n):
        s = total * k / n
        while acc + seg[i] < s:
            acc += seg[i]
            i += 1
        a, b = poly[i], poly[(i + 1) % len(poly)]
        f = (s - acc) / seg[i] if seg[i] else 0.0
        p = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        # Tangente ueber Nachbarpunkte glätten
        pa, pb = poly[(i - 2) % len(poly)], poly[(i + 3) % len(poly)]
        t = Vector((pb[0] - pa[0], pb[1] - pa[1])).normalized()
        out.append((p, (t.x, t.y)))
    return out


def build_track_link():
    """Einzelnes Kettenglied: lokal X = Laufrichtung, Z = nach aussen (Boden), Y = quer."""
    p = Part("SM_L2A8_TrackLink", "root", "track", bevel=0.0)
    hp = TRACK_PITCH / 2
    p.box_mm((-hp + 0.012, -0.25, -0.045), (hp - 0.012, 0.25, 0.012), "metal")          # Kettenkoerper
    for y0, y1 in ((0.035, 0.235), (-0.235, -0.035)):
        p.box_mm((-hp + 0.025, y0, 0.012), (hp - 0.025, y1, 0.045), "rubber")         # Bodenpolster
        p.box_mm((-hp + 0.03, y0 + 0.01, -0.055), (hp - 0.03, y1 - 0.01, -0.045), "rubber")  # Laufflaeche innen
    for y in (-1, 1):   # Endverbinder (einer pro Gelenk)
        p.box_mm((hp - 0.035, min(y * 0.25, y * 0.3175), -0.035), (hp + 0.035, max(y * 0.25, y * 0.3175), 0.020), "metal")
    for x in (-hp + 0.02, hp - 0.02):
        p.cyl((x, -0.30, -0.012), (x, 0.30, -0.012), 0.02, seg=8, surf="metal")
    # Fuehrungszahn (laeuft zwischen den Doppelrollen)
    p.hull([(x, y, -0.045) for x in (-0.05, 0.05) for y in (-0.035, 0.035)] +
           [(x, y, -0.135) for x in (-0.03, 0.03) for y in (-0.025, 0.025)], "metal")
    return p


# Querschnitt des Kettenbands (Abstand nach aussen n, Querlage y); U laeuft einmal um den Querschnitt
BELT_SEC = [(-TRACK_T / 2, -TRACK_W / 2 + 0.04), (-TRACK_T / 2, TRACK_W / 2 - 0.04), (-0.01, TRACK_W / 2),
            (TRACK_T / 2, TRACK_W / 2 - 0.03), (TRACK_T / 2, -TRACK_W / 2 + 0.03), (-0.01, -TRACK_W / 2)]


def belt_ucum():
    n = len(BELT_SEC)
    lens = [math.dist(BELT_SEC[i], BELT_SEC[(i + 1) % n]) for i in range(n)]
    return [sum(lens[:i]) / sum(lens) for i in range(n + 1)]


def build_tracks(coll_links, coll_belt, mats):
    poly, seg, total = track_path()
    n_links = round(total / TRACK_PITCH)
    pitch = total / n_links
    tmpl = finalize(build_track_link(), coll_links, mats)
    unwrap([tmpl], margin=0.01)
    link_objs, belt_objs, path_data = [], [], {}
    for s, side in ((1, "L"), (-1, "R")):
        yc = s * TRACK_Y
        # --- Variante A: Einzelglieder (Hochdetail, statischer Export) ---
        copies = []
        for (px, pz), (tx, tz) in sample_path(poly, seg, total, n_links):
            X = Vector((tx, 0, tz))
            Z = Vector((tz, 0, -tx))            # Aussennormale fuer CCW-Pfad
            Y = Z.cross(X)
            m = Matrix((X, Y, Z)).transposed().to_4x4()
            m.translation = (px, yc, pz)
            o = bpy.data.objects.new("tmp_link", tmpl.data)
            coll_links.objects.link(o)
            o.matrix_world = m
            copies.append(o)
        base = copies[0]
        base.data = tmpl.data.copy()
        with bpy.context.temp_override(active_object=base, selected_editable_objects=copies,
                                       selected_objects=copies, object=base):
            bpy.ops.object.join()
        base.name = base.data.name = f"Track_{side}_Links"
        base["bone"], base["group"] = "root", "track"
        # Pivot auf Weltursprung legen
        mw = base.matrix_world.copy()
        base.data.transform(mw)
        base.matrix_world = Matrix.Identity(4)
        link_objs.append(base)

        # --- Variante B: geschlossenes Kettenband fuer UE5 (UV-Panner) ---
        samples = sample_path(poly, seg, total, n_links * 2)
        sec = BELT_SEC
        bm = bmesh.new()
        rings = []
        for (px, pz), (tx, tz) in samples:
            nrm = Vector((tz, -tx))
            rings.append([bm.verts.new((px + nrm.x * n, yc + y, pz + nrm.y * n)) for n, y in sec])
        uv = bm.loops.layers.uv.new("UVMap")
        ucum = belt_ucum()
        nr = len(rings)
        for k in range(nr):
            A, B = rings[k], rings[(k + 1) % nr]
            v0, v1 = k / 2.0, (k + 1) / 2.0     # 1 UV-Kachel in V = 1 Kettenglied
            for i in range(6):
                j = (i + 1) % 6
                f = bm.faces.new((A[i], A[j], B[j], B[i]))
                for loop, (u, v) in zip(f.loops, ((ucum[i], v0), (ucum[i + 1], v0), (ucum[i + 1], v1), (ucum[i], v1))):
                    loop[uv].uv = (u, v)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        me = bpy.data.meshes.new(f"Track_{side}_Belt")
        bm.to_mesh(me)
        bm.free()
        me.materials.append(mats["track_belt"])
        me.shade_smooth()
        me.set_sharp_from_angle(angle=math.radians(40))
        col = me.color_attributes.new("ColorID", "FLOAT_COLOR", "CORNER")
        col.data.foreach_set("color", [*SURF_COLOR[1], 1.0] * len(me.loops))
        for attr, val in (("paint_mask", 0.0), ("roughness", 0.55), ("metallic", 0.7)):
            me.attributes.new(attr, "FLOAT", "FACE").data.foreach_set("value", [val] * len(me.polygons))
        o = bpy.data.objects.new(f"Track_{side}_Belt", me)
        o["bone"], o["group"] = "root", "track"
        coll_belt.objects.link(o)
        belt_objs.append(o)

        # Pfad fuer UE5-Splines: cm, UE-Koordinaten (Y gespiegelt)
        path_data[side] = {
            "y_cm": round(-yc * 100, 2),
            "points_xz_cm": [[round(px * 100, 2), round(pz * 100, 2)] for (px, pz), _ in samples[::2]],
        }
    tmpl.name = "SM_L2A8_TrackLink"
    tmpl.hide_render = tmpl.hide_viewport = True     # Vorlage fuer Export und Textur-Bake
    info = {"link_count_per_side": n_links, "link_pitch_cm": round(pitch * 100, 3),
            "track_length_cm": round(total * 100, 2), "sides": path_data}
    return link_objs, belt_objs, info, tmpl


# =============================================================================
# Turm
# =============================================================================
def build_turret():
    parts = []
    tp = Vector((TURRET_X, 0, TURRET_Z))
    t = Part("Turret_Body", "turret", "turret", pivot=tp, bevel=0.02, weld=True)
    # Grundturm (Draufsicht: vorn schmaler, Mitte am breitesten), Seiten oben leicht eingezogen
    t.hull([(x, s * y, TURRET_Z) for x, y in ((1.85, 1.40), (0.60, 1.52), (-0.90, 1.48)) for s in (-1, 1)] +
           [(x, s * y, ROOF_Z) for x, y in ((1.65, 1.36), (0.60, 1.48), (-0.90, 1.44)) for s in (-1, 1)])
    # Turmheck (Bustle) mit hoeher liegendem Boden
    t.hull([(x, s * y, z) for x, y, z in ((-0.90, 1.46, 1.98), (-1.80, 1.24, 1.98), (-1.88, 1.20, 2.08),
                                           (-0.90, 1.44, ROOF_Z - 0.01), (-1.90, 1.20, ROOF_Z - 0.03))
            for s in (-1, 1)])
    # Turmdrehkranz-Unterteil
    t.cyl((TURRET_X, 0, TURRET_Z - 0.01), (TURRET_X, 0, TURRET_Z + 0.05), 1.10, seg=64, surf="dark")
    parts.append(t)

    a = Part("Turret_Armor", "turret", "turret", pivot=tp, bevel=0.015, weld=True)
    for s in (-1, 1):
        # Keilpanzerung (seit 2A5): Pfeil in Drauf- und Seitenansicht, Spitze etwa auf Rohrhoehe
        a.hull([(3.22, s * 0.42, GUN_Z - 0.05), (3.22, s * 0.42, GUN_Z + 0.03),
                (2.52, s * 1.44, GUN_Z - 0.10), (2.52, s * 1.44, GUN_Z + 0.02),
                (1.55, s * 0.42, ROOF_Z - 0.04), (1.55, s * 1.44, ROOF_Z - 0.04),
                (1.80, s * 0.42, TURRET_Z + 0.02), (1.80, s * 1.44, TURRET_Z + 0.02)])
        # Seitenmodule: Keil laeuft an der Turmseite nach hinten aus
        a.hull([(0.30, s * y, z) for y in (1.46, 1.58) for z in (1.80, 2.55)] +
               [(2.00, s * y, z) for y in (1.40, 1.54) for z in (1.84, 2.50)] +
               [(2.50, s * 1.46, GUN_Z - 0.06), (2.50, s * 1.46, GUN_Z + 0.02)])
        a.bolts([(x, s * 1.58, z) for x in (0.55, 1.05, 1.55) for z in (1.95, 2.40)], (0, s, 0), r=0.025, h=0.012)
        # Dachpanzerung vorn
        a.hull([(x, s * y, ROOF_Z) for x in (0.85, 1.62) for y in (0.45, 1.34)] +
               [(x, s * y, ROOF_Z + 0.05) for x in (0.95, 1.50) for y in (0.50, 1.28)])
    parts.append(a)

    # Blende mit Blendenhuelle (gehoert zur Wiege, laeuft nicht mit zurueck)
    gp = Vector((GUN_X, 0, GUN_Z))
    m = Part("Gun_Mantlet", "gun", "gun", pivot=gp, bevel=0.015)
    m.hull([(1.70, s * 0.40, GUN_Z + dz) for s in (-1, 1) for dz in (-0.26, 0.24)] +
           [(3.05, s * 0.32, GUN_Z + dz) for s in (-1, 1) for dz in (-0.20, 0.18)])
    m.cyl((3.03, 0.25, GUN_Z - 0.12), (3.09, 0.25, GUN_Z - 0.12), 0.045, seg=16, surf="dark")   # Koax-MG
    m.lathe((0, 0, GUN_Z), (1, 0, 0), [(2.90, 0.0), (2.90, 0.20), (3.00, 0.215), (3.95, 0.205),
                                       (4.05, 0.19), (4.05, 0.0)], seg=40)
    # Gummi-Staubschutz zwischen Blende und Blendenhuelle
    m.lathe((0, 0, GUN_Z), (1, 0, 0), [(2.98, 0.0), (2.98, 0.255), (3.00, 0.265), (3.04, 0.245), (3.07, 0.262),
                                       (3.10, 0.240), (3.12, 0.0)], seg=40, surf="rubber")
    parts.append(m)

    # Waffenrohr Rh-120 L/55A1 inkl. Waermeschutzhuelle, Rauchabsauger, Muendung
    b = Part("Gun_Barrel", "barrel", "gun", pivot=gp, bevel=0.004)
    prof = [(3.90, 0.0), (3.90, 0.155), (4.80, 0.150), (4.80, 0.158), (4.86, 0.158), (4.86, 0.148),
            (4.92, 0.148), (5.02, 0.196), (5.78, 0.196), (5.88, 0.142), (6.30, 0.134), (6.30, 0.142),
            (6.36, 0.142), (6.36, 0.132), (6.80, 0.120), (6.82, 0.124), (MUZZLE_X, 0.124),
            (MUZZLE_X, BORE_R), (MUZZLE_X - 0.5, BORE_R), (MUZZLE_X - 0.5, 0.0)]
    ps = ["paint"] * 15 + ["metal", "metal", "dark", "dark"]
    b.lathe((0, 0, GUN_Z), (1, 0, 0), prof, seg=40, surf=ps)
    # Muendungsreferenzsystem
    b.box((MUZZLE_X - 0.22, 0, GUN_Z + 0.155), (0.16, 0.07, 0.06), "paint")
    b.box((MUZZLE_X - 0.30, 0, GUN_Z + 0.13), (0.04, 0.05, 0.04), "metal")
    parts.append(b)

    # Optiken: Richtschuetze (EMES 15, hinter dem rechten Keil) und Kommandant (PERI R17A3)
    o = Part("Turret_Optics", "turret", "turret", pivot=tp, bevel=0.01)
    o.hull([(x, y, ROOF_Z - 0.02) for x in (1.20, 1.88) for y in (-0.42, -0.95)] +
           [(x, y, 2.86) for x in (1.25, 1.80) for y in (-0.44, -0.93)])
    o.box((1.86, -0.685, 2.73), (0.03, 0.36, 0.13), "glass", rot=(0, -12, 0))
    o.hull([(x, y, z) for x in (1.74, 1.92) for y in (-0.41, -0.96) for z in (2.84, 2.89)])   # Panzerklappe
    px, py = PERI_POS
    o.cyl((px, py, ROOF_Z - 0.02), (px, py, 2.74), 0.22, seg=32)
    o.hull([(x, y, z) for x in (px - 0.22, px + 0.20) for y in (py + 0.16, py - 0.16) for z in (2.74, 2.98)] +
           [(px + 0.24, y, z) for y in (py + 0.14, py - 0.14) for z in (2.78, 2.94)])
    o.box((px + 0.251, py, 2.86), (0.012, 0.22, 0.11), "glass")
    o.box((px, py, 2.99), (0.30, 0.30, 0.02))
    # Winkelspiegel um die Kommandantenluke (nicht in Richtung PERI)
    hx, hy, hr = CMD_HATCH
    to_peri = math.atan2(py - hy, px - hx)
    for k in range(8):
        a_ = k * math.pi / 4 + math.pi / 8
        if abs((a_ - to_peri + math.pi) % (2 * math.pi) - math.pi) < math.radians(40):
            continue
        c = Vector((hx + math.cos(a_) * (hr + 0.09), hy + math.sin(a_) * (hr + 0.09), ROOF_Z + 0.06))
        o.box(c, (0.10, 0.16, 0.11), rot=(0, 0, math.degrees(a_)))
        o.box(c + Vector((math.cos(a_) * 0.052, math.sin(a_) * 0.052, 0.01)), (0.006, 0.12, 0.05), "glass",
              rot=(0, 0, math.degrees(a_)))
    parts.append(o)

    # Luken (eigene Bones, oeffnen um die hintere Kante)
    for name, bone, (cx, cy, r) in (("Hatch_Commander", "hatch_commander", CMD_HATCH),
                                    ("Hatch_Loader", "hatch_loader", LDR_HATCH)):
        hinge = Vector((cx - r, cy, ROOF_Z + 0.06))
        h = Part(f"Turret_{name}", bone, "turret", pivot=hinge, bevel=0.008)
        h.cyl((cx, cy, ROOF_Z - 0.01), (cx, cy, ROOF_Z + 0.05), r + 0.05, seg=40)
        h.cyl((cx, cy, ROOF_Z + 0.05), (cx, cy, ROOF_Z + 0.12), r, r - 0.02, seg=40)
        h.box((cx - r - 0.02, cy, ROOF_Z + 0.07), (0.08, 0.30, 0.06), "metal")
        h.beam((cx - 0.15, cy - 0.12, ROOF_Z + 0.15), (cx + 0.15, cy - 0.12, ROOF_Z + 0.15), 0.025, 0.025, "metal")
        parts.append(h)

    # Fernbedienbare Waffenstation (FLW 200)
    rws = Vector((*RWS_POS, ROOF_Z))
    w = Part("RWS_Base", "rws_yaw", "turret", pivot=rws, bevel=0.01)
    w.cyl(rws, rws + Vector((0, 0, 0.12)), 0.22, seg=32)
    w.hull([rws + Vector((x, y, z)) for x in (-0.30, 0.22) for y in (-0.22, 0.22) for z in (0.12, 0.42)] +
           [rws + Vector((0.28, y, z)) for y in (-0.18, 0.18) for z in (0.16, 0.36)])
    w.box(rws + Vector((-0.05, 0.27, 0.32)), (0.36, 0.10, 0.24))           # Munitionskasten
    parts.append(w)
    rp = rws + Vector((0.0, -0.30, 0.30))
    wp = Part("RWS_Cradle", "rws_pitch", "turret", pivot=rp, bevel=0.006)
    wp.box(rp + Vector((0.10, 0, 0.0)), (0.62, 0.14, 0.16))                 # MG-Gehaeuse
    wp.cyl(rp + Vector((0.40, 0, 0.02)), rp + Vector((1.05, 0, 0.02)), 0.025, seg=12, surf="metal")
    wp.cyl(rp + Vector((1.00, 0, 0.02)), rp + Vector((1.08, 0, 0.02)), 0.035, seg=12, surf="metal")
    wp.box(rp + Vector((0.12, -0.15, 0.08)), (0.30, 0.14, 0.22))           # Sensorkopf
    wp.box(rp + Vector((0.272, -0.15, 0.08)), (0.006, 0.10, 0.14), "glass")
    parts.append(wp)

    # Trophy-Schutzsystem (2A8): Radarkaesten an den Turmseiten, Werfer obenauf
    for s, side in ((1, "l"), (-1, "r")):
        tc = Vector((TROPHY_POS[0], s * TROPHY_POS[1], ROOF_Z + 0.02))
        tr = Part(f"Trophy_Launcher_{side.upper()}", f"trophy_{side}", "turret", pivot=tc, bevel=0.01)
        tr.cyl(tc, tc + Vector((0, 0, 0.10)), 0.18, seg=32)
        tr.hull([tc + Vector((x, y, z)) for x in (-0.22, 0.22) for y in (-0.15, 0.15) for z in (0.10, 0.34)] +
                [tc + Vector((0.28, y, z)) for y in (-0.11, 0.11) for z in (0.14, 0.30)])
        tr.box(tc + Vector((0.0, s * 0.155, 0.22)), (0.28, 0.012, 0.15), "dark")
        parts.append(tr)
    rd = Part("Turret_Trophy_Radars", "turret", "turret", pivot=tp, bevel=0.008)
    for s in (-1, 1):
        # hinterer Radarkasten (Gitterflaeche nach aussen)
        yi, yo = s * 1.32, s * 1.66
        rd.hull([(x, y, z) for x in (-0.72, -1.58) for y in (yi, yo) for z in (1.94, ROOF_Z + 0.02)] +
                [(-0.62, yi, z) for z in (1.98, ROOF_Z - 0.02)])
        rd.box((-1.15, s * 1.665, 2.27), (0.70, 0.012, 0.44), "dark")
        for k in range(6):
            rd.box((-0.85 - k * 0.12, s * 1.672, 2.27), (0.012, 0.012, 0.44), "metal")
        for k in range(4):
            rd.box((-1.15, s * 1.672, 2.10 + k * 0.11), (0.70, 0.012, 0.012), "metal")
        # vorderes Radarpaneel, schraeg nach vorn-aussen
        c, yaw = Vector((0.18, s * 1.64, 2.22)), s * 55
        rot = (0, 0, yaw - 90 * s)
        rd.box(c, (0.44, 0.09, 0.38), rot=rot)
        n = Vector((math.cos(math.radians(yaw)), math.sin(math.radians(yaw)), 0))
        rd.box(c + n * 0.046, (0.36, 0.006, 0.30), "dark", rot=rot)
        rd.beam(c - n * 0.04 + Vector((0, 0, -0.19)), c - n * 0.14 + Vector((0, 0, -0.30)), 0.05, 0.05)
    parts.append(rd)

    # Ausstattung: Nebelwurfbecher, Antennen, Windsensor, Staukoerbe am Turmheck
    x = Part("Turret_Details", "turret", "turret", pivot=tp, bevel=0.006)
    for s in (-1, 1):
        # Nebelwurfbecher: 4er-Block an der Turmseite zwischen Seitenmodul und Trophy
        x.box((-0.27, s * 1.56, 2.40), (0.52, 0.14, 0.14))
        for k in range(4):
            base = Vector((-0.46 + k * 0.125, s * 1.58, 2.46))
            d = Vector((0.25, s * 0.40, 0.88)).normalized()
            x.cyl(base, base + d * 0.26, 0.047, seg=16)
            x.cyl(base + d * 0.26, base + d * 0.265, 0.04, seg=16, surf="dark")
        # Antennen
        ab = Vector((-1.70, s * 0.95, ROOF_Z - 0.03))
        x.cyl(ab, ab + Vector((0, 0, 0.14)), 0.055, seg=16)
        x.cyl(ab + Vector((0, 0, 0.14)), ab + Vector((0, 0, 1.95)), 0.010, 0.005, seg=6, surf="dark")
        # Staukorb an der hinteren Turmecke: Rohrrahmen, Gitter aussen, Gepaeck
        y0, y1 = s * 0.55, s * 1.20
        for z in (2.02, 2.54):
            x.beam((-1.86, y1, z), (-2.80, y1, z), 0.035, 0.035, "metal")
            x.beam((-2.80, y0, z), (-2.80, y1, z), 0.035, 0.035, "metal")
            x.beam((-1.86, y0, z), (-2.80, y0, z), 0.035, 0.035, "metal")
        for px_ in (-2.80, -2.33):
            for py_ in (y0, y1):
                x.beam((px_, py_, 2.00), (px_, py_, 2.56), 0.035, 0.035, "metal")
        x.box((-2.33, y1, 2.28), (0.94, 0.008, 0.50), "dark")
        x.box((-2.80, (y0 + y1) / 2, 2.28), (0.008, abs(y1 - y0), 0.50), "dark")
        x.box((-2.30, (y0 + y1) / 2, 2.18), (0.80, 0.55, 0.30))
        x.box((-2.45, (y0 + y1) / 2 - s * 0.05, 2.42), (0.45, 0.40, 0.20))
    # Windsensor
    x.cyl((-1.60, -0.30, ROOF_Z - 0.02), (-1.60, -0.30, 3.00), 0.02, seg=8, surf="metal")
    x.box((-1.60, -0.30, 3.02), (0.10, 0.10, 0.06))
    parts.append(x)

    # Anbauteile Turm: Griffe, Hebeoesen, Tarnnetzhaken
    f = Part("Turret_Fittings", "turret", "turret", pivot=tp, bevel=0.0)
    for s in (-1, 1):
        f.eye((1.20, s * 1.24, ROOF_Z), (0, 0, 1), (1, 0, 0))
        f.eye((-1.70, s * 0.95 + s * 0.20, ROOF_Z - 0.03), (0, 0, 1), (1, 0, 0))
        f.handle((0.55, s * 1.40, ROOF_Z), (0.85, s * 1.40, ROOF_Z), (0, 0, 1))
        f.handle((-1.90, s * 0.25, 2.30), (-1.90, s * 0.55, 2.30), (-1, 0, 0))
        for x in (0.55, 0.90, 1.25, 1.60):
            base = Vector((x, s * 1.575, 2.52))
            f.cyl(base, base + Vector((0, s * 0.05, 0.0)), 0.012, seg=6)
            f.cyl(base + Vector((0, s * 0.05, 0.0)), base + Vector((0, s * 0.05, 0.035)), 0.012, seg=6)
    parts.append(f)
    return parts


# =============================================================================
# Rig
# =============================================================================
def build_armature(coll):
    tp = (TURRET_X, 0, TURRET_Z)
    gp = (GUN_X, 0, GUN_Z)
    up, fwd, lat = (0, 0, 0.3), (0.4, 0, 0), (0, 0.3, 0)
    rws = (*RWS_POS, ROOF_Z)
    bones = [
        ("root", None, (0, 0, 0), up),
        ("turret", "root", tp, up),
        ("gun", "turret", gp, fwd),
        ("barrel", "gun", gp, fwd),
        ("muzzle", "barrel", (MUZZLE_X, 0, GUN_Z), fwd),
        ("coax", "gun", (3.09, 0.25, GUN_Z - 0.12), fwd),
        ("hatch_commander", "turret", (CMD_HATCH[0] - CMD_HATCH[2], CMD_HATCH[1], ROOF_Z + 0.06), up),
        ("hatch_loader", "turret", (LDR_HATCH[0] - LDR_HATCH[2], LDR_HATCH[1], ROOF_Z + 0.06), up),
        ("rws_yaw", "turret", rws, up),
        ("rws_pitch", "rws_yaw", (rws[0], rws[1] - 0.30, rws[2] + 0.30), fwd),
        ("rws_muzzle", "rws_pitch", (rws[0] + 1.08, rws[1] - 0.30, rws[2] + 0.32), fwd),
        ("trophy_l", "turret", (TROPHY_POS[0], TROPHY_POS[1], ROOF_Z + 0.02), up),
        ("trophy_r", "turret", (TROPHY_POS[0], -TROPHY_POS[1], ROOF_Z + 0.02), up),
        ("exhaust_l", "root", (HULL_X0 - 0.05, 1.27, 1.45), (-0.3, 0, 0)),
        ("exhaust_r", "root", (HULL_X0 - 0.05, -1.27, 1.45), (-0.3, 0, 0)),
    ]
    for s, side in ((1, "l"), (-1, "r")):
        y = s * TRACK_Y
        for i, x in enumerate(RW_X, 1):
            bones.append((f"wheel_{side}_{i:02d}", "root", (x, y, RW_Z), lat))
        bones.append((f"idler_{side}", "root", (IDLER[0], y, IDLER[1]), lat))
        bones.append((f"sprocket_{side}", "root", (SPROCKET[0], y, SPROCKET[1]), lat))
        for i, x in enumerate(ROLLER_X, 1):
            bones.append((f"roller_{side}_{i:02d}", "root", (x, y, ROLLER_Z), lat))

    data = bpy.data.armatures.new("Armature")
    arm = bpy.data.objects.new("Armature", data)   # Name "Armature" -> UE5 legt keinen Extra-Root an
    coll.objects.link(arm)
    data.display_type = "STICK"
    arm.show_in_front = True
    vl = bpy.context.view_layer
    vl.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    for name, parent, head, tail in bones:
        eb = data.edit_bones.new(name)
        eb.head = head
        eb.tail = Vector(head) + Vector(tail)
        if parent:
            eb.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    return arm, [b[0] for b in bones]


def skin(objs, arm):
    for o in objs:
        vg = o.vertex_groups.new(name=o["bone"])
        vg.add(list(range(len(o.data.vertices))), 1.0, "REPLACE")
        mod = o.modifiers.new("Armature", "ARMATURE")
        mod.object = arm
        o.parent = arm
        o.matrix_parent_inverse = arm.matrix_world.inverted()


# =============================================================================
# UVs
# =============================================================================
def unwrap(objs, margin=0.003):
    if not objs:
        return
    vl = bpy.context.view_layer
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    vl.objects.active = objs[0]
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(60), island_margin=margin,
                             area_weight=0.0, correct_aspect=True, scale_to_bounds=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")


def unwrap_shared(objs):
    """Baugleiche Raeder teilen sich die UVs (hoehere Texelichte): nur Vorlagen auspacken, dann kopieren."""
    by_name = {o.name: o for o in objs}

    def template(name):
        for prefix, tmpl in (("RoadWheel_", "RoadWheel_L_01"), ("ReturnRoller_", "ReturnRoller_L_01"),
                             ("Idler_", "Idler_L"), ("Sprocket_", "Sprocket_L")):
            if name.startswith(prefix):
                return tmpl
        return name
    uniques = [o for o in objs if template(o.name) == o.name]
    unwrap(uniques)
    for o in objs:
        t = by_name[template(o.name)]
        if t is o:
            continue
        src, dst = t.data, o.data
        assert len(src.loops) == len(dst.loops), (t.name, o.name)
        if not dst.uv_layers:
            dst.uv_layers.new(name="UVMap")
        uv = [0.0] * (2 * len(src.loops))
        src.uv_layers[0].data.foreach_get("uv", uv)
        dst.uv_layers[0].data.foreach_set("uv", uv)
        o["uv_template"] = t.name


# =============================================================================
# Export
# =============================================================================
FBX_COMMON = dict(
    use_selection=True, apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL",
    axis_forward="-Z", axis_up="Y", use_mesh_modifiers=True, mesh_smooth_type="FACE",
    use_tspace=False, colors_type="SRGB", add_leaf_bones=False, primary_bone_axis="Y",
    secondary_bone_axis="X", armature_nodetype="NULL", bake_anim=False, path_mode="AUTO",
)


def select_only(objs):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def export_skeletal(path, arm, meshes):
    select_only([arm] + meshes)
    bpy.ops.export_scene.fbx(filepath=path, object_types={"ARMATURE", "MESH"}, **FBX_COMMON)


def joined_copy(objs, name, coll):
    copies = []
    for o in objs:
        c = o.copy()
        c.data = o.data.copy()
        coll.objects.link(c)
        c.parent = None
        c.matrix_world = o.matrix_world.copy()
        c.modifiers.clear()
        c.vertex_groups.clear()
        copies.append(c)
    with bpy.context.temp_override(active_object=copies[0], selected_editable_objects=copies,
                                   selected_objects=copies, object=copies[0]):
        bpy.ops.object.join()
    j = copies[0]
    j.data.transform(j.matrix_world)
    j.matrix_world = Matrix.Identity(4)
    j.name = j.data.name = name
    return j


def ucx(name, objs, coll, extra=()):
    bm = bmesh.new()
    pts = [o.matrix_world @ v.co for o in objs for v in o.data.vertices] + [Vector(p) for p in extra]
    vs = [bm.verts.new(p) for p in pts]
    res = bmesh.ops.convex_hull(bm, input=vs)
    junk = {g for g in res["geom_interior"] + res["geom_unused"] if isinstance(g, bmesh.types.BMVert)}
    bmesh.ops.delete(bm, geom=list(junk), context="VERTS")
    # UE begrenzt Kollisions-Huellen auf wenige Vertices -> vereinfachen
    bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(8), verts=bm.verts[:], edges=bm.edges[:])
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    coll.objects.link(o)
    o.display_type = "WIRE"
    return o


# =============================================================================
# Vorschau-Rendering
# =============================================================================
def setup_preview(coll):
    s = bpy.context.scene
    s.render.engine = "CYCLES"
    s.cycles.device = "CPU"
    s.render.resolution_x, s.render.resolution_y = 1600, 900
    s.render.image_settings.file_format = "JPEG"
    s.render.image_settings.quality = 90
    s.view_settings.view_transform = "AgX"
    s.view_settings.exposure = -1.2
    world = bpy.data.worlds.new("World")
    s.world = world
    if world.node_tree is None:
        world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes.get("Background") or nt.nodes.new("ShaderNodeBackground")
    sky = nt.nodes.new("ShaderNodeTexSky")
    try:
        sky.sky_type = "NISHITA"
    except TypeError:
        pass
    nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = 0.30
    if not any(n.type == "OUTPUT_WORLD" for n in nt.nodes):
        out = nt.nodes.new("ShaderNodeOutputWorld")
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    sun = bpy.data.lights.new("Sun", "SUN")
    sun.energy = 3.5
    sun.angle = math.radians(2)
    so = bpy.data.objects.new("Sun", sun)
    so.rotation_euler = (math.radians(40), math.radians(10), math.radians(-35))
    coll.objects.link(so)
    gm = bpy.data.materials.new("Ground")
    _, bsdf = _new_nodetree(gm)
    bsdf.inputs["Base Color"].default_value = (0.12, 0.105, 0.085, 1)
    bsdf.inputs["Roughness"].default_value = 0.95
    bpy.ops.mesh.primitive_plane_add(size=80)
    g = bpy.context.active_object
    g.name = "Ground"
    g.data.materials.append(gm)
    for c in g.users_collection:
        c.objects.unlink(g)
    coll.objects.link(g)
    cam = bpy.data.cameras.new("Cam")
    co = bpy.data.objects.new("Cam", cam)
    coll.objects.link(co)
    s.camera = co
    tgt = bpy.data.objects.new("CamTarget", None)
    coll.objects.link(tgt)
    con = co.constraints.new("TRACK_TO")
    con.target = tgt
    return co, tgt


def render_views(outdir, arm, samples, hide_objs):
    os.makedirs(outdir, exist_ok=True)
    s = bpy.context.scene
    s.cycles.samples = samples
    try:
        s.cycles.use_denoising = True
    except AttributeError:
        pass
    coll = get_collection("Preview")
    cam, tgt = setup_preview(coll)
    for o in hide_objs:
        o.hide_render = True
    pb = arm.pose.bones
    views = [
        ("leopard2a8_front34.jpg", (11.5, 8.5, 3.6), (0.6, 0, 1.3), 40, None),
        ("leopard2a8_rear34.jpg", (-10.5, -8.0, 4.2), (0.0, 0, 1.3), 40, (35, -6)),
        ("leopard2a8_side_ortho.jpg", (1.6, 25, 1.55), (1.6, 0, 1.55), None, None),
        ("leopard2a8_top_ortho.jpg", (1.6, 0, 25), (1.6, 0, 0), None, None),
        ("leopard2a8_detail_turret.jpg", (5.2, -4.2, 3.9), (0.6, 0, 2.3), 45, None),
    ]
    for fname, loc, look, lens, pose in views:
        cam.location = loc
        tgt.location = look
        if lens is None:
            cam.data.type = "ORTHO"
            cam.data.ortho_scale = 12.5
        else:
            cam.data.type = "PERSP"
            cam.data.lens = lens
        pb["turret"].rotation_mode = pb["gun"].rotation_mode = "XYZ"
        if pose:
            pb["turret"].rotation_euler = (0, math.radians(pose[0]), 0)   # Bone-Y zeigt nach oben
            pb["gun"].rotation_euler = (0, 0, math.radians(pose[1]))
        else:
            pb["turret"].rotation_euler = (0, 0, 0)
            pb["gun"].rotation_euler = (0, 0, 0)
        s.render.filepath = os.path.join(outdir, fname)
        bpy.ops.render.render(write_still=True)
        print("Render:", s.render.filepath)
    pb["turret"].rotation_euler = (0, 0, 0)
    pb["gun"].rotation_euler = (0, 0, 0)
    for o in hide_objs:
        o.hide_render = False
    return coll


# =============================================================================
# Main
# =============================================================================
def parse_args():
    if "--" in sys.argv:
        argv = sys.argv[sys.argv.index("--") + 1:]
    elif os.path.basename(sys.argv[0]).endswith(".py"):
        argv = sys.argv[1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="export")
    p.add_argument("--renders", default="renders")
    p.add_argument("--no-export", action="store_true")
    p.add_argument("--no-render", action="store_true")
    p.add_argument("--samples", type=int, default=48)
    p.add_argument("--bake", action="store_true", help="PBR-Texturen backen (BaseColor, Normal, ORM)")
    p.add_argument("--tex-size", type=int, default=2048)
    p.add_argument("--bake-samples", type=int, default=64)
    return p.parse_args(argv)


def bake_textures(args, out, mats, hull, turret, gear, link_tmpl, track_info):
    if bake_l2a8 is None:
        raise RuntimeError("bake_l2a8.py nicht gefunden (muss neben diesem Skript liegen)")
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    if sc.world is None:
        sc.world = bpy.data.worlds.new("World")
    sc.world.light_settings.distance = 0.5          # Reichweite fuer den AO-Bake
    tex_dir = os.path.join(out, "textures")
    size = args.tex_size
    pitch = track_info["link_pitch_cm"] / 100
    sets = {
        "hull": (hull, size, None),
        "turret": ([o for o in turret if o["group"] == "turret"], size, None),
        "gun": ([o for o in turret if o["group"] == "gun"], size // 2, None),
        "wheels": ([o for o in gear if "uv_template" not in o], size, None),
        "track": ([link_tmpl], size // 4, pitch),
    }
    paths = {}
    for group, (objs, sz, chain) in sets.items():
        paths[group] = bake_l2a8.bake_set(group, objs, tex_dir, sz, args.bake_samples, chain_pitch=chain)
    paths["track_belt"] = bake_l2a8.bake_belt_tile(link_tmpl, pitch, BELT_SEC, belt_ucum(), tex_dir,
                                                   size // 4, args.bake_samples)
    for group, p in paths.items():
        bake_l2a8.textured_material(mats[group], p)
    c = bpy.data.collections.get("L2A8_Bake")
    if c:
        for o in list(c.objects):
            bpy.data.objects.remove(o)
        bpy.data.collections.remove(c)


def tri_count(objs):
    return sum(len(p.vertices) - 2 for o in objs for p in o.data.polygons)


def main():
    args = parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    reset_scene()
    mats = make_materials()

    root = get_collection("Leopard2A8")
    c_hull = get_collection("L2A8_Hull", root)
    c_turret = get_collection("L2A8_Turret", root)
    c_gear = get_collection("L2A8_RunningGear", root)
    c_links = get_collection("L2A8_Tracks_Links (Hochdetail)", root)
    c_belt = get_collection("L2A8_Tracks_Belt (UE5 Skeletal)", root)
    c_rig = get_collection("L2A8_Rig", root)

    hull = [finalize(p, c_hull, mats) for p in build_hull()]
    gear = [finalize(p, c_gear, mats) for p in build_running_gear()]
    turret = [finalize(p, c_turret, mats) for p in build_turret()]
    links, belts, track_info, link_tmpl = build_tracks(c_links, c_belt, mats)

    # UV-Atlanten je Texture-Set
    unwrap(hull)
    unwrap([o for o in turret if o["group"] == "turret"])
    unwrap([o for o in turret if o["group"] == "gun"])
    unwrap_shared(gear)

    arm, bone_names = build_armature(c_rig)
    skin(hull + gear + turret + links + belts, arm)

    all_static = hull + gear + turret
    if args.bake:
        bake_textures(args, out, mats, hull, turret, gear, link_tmpl, track_info)
    info = {
        "note": "Naeherungsmodell aus oeffentlichen Daten - keine Herstellermasse.",
        "bones": bone_names,
        "triangles": {
            "hull": tri_count(hull), "turret_gun": tri_count(turret), "running_gear": tri_count(gear),
            "track_links_hd": tri_count(links), "track_belt_ue": tri_count(belts),
        },
        "track": track_info,
    }
    # Gemessene Hauptmasse
    pts = [o.matrix_world @ v.co for o in all_static + links for v in o.data.vertices]
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    hull_pts = [o.matrix_world @ v.co for o in hull for v in o.data.vertices]
    info["dimensions_m"] = {
        "length_gun_forward": round(mx.x - mn.x, 3),
        "hull_length": round(max(p.x for p in hull_pts) - min(p.x for p in hull_pts), 3),
        "width_overall": round(mx.y - mn.y, 3),
        "height_overall_excl_antennas": None,
        "ground_clearance": round(min(p.z for p in hull_pts), 3),
    }
    no_ant = [o.matrix_world @ v.co for o in turret if o.name != "Turret_Details" for v in o.data.vertices]
    info["dimensions_m"]["height_overall_excl_antennas"] = round(max(p.z for p in no_ant), 3)

    if not args.no_export:
        sk_meshes = hull + gear + turret + belts
        export_skeletal(os.path.join(out, "SK_Leopard2A8.fbx"), arm, sk_meshes)

        # Statisches Mesh (Nanite) mit Einzelgliedern + UCX-Kollision
        tmp = get_collection("tmp_export")
        sm = joined_copy(all_static + links, "SM_Leopard2A8", tmp)
        body = [o for o in hull if o.name in ("Hull_Body", "Hull_Armor")] + [o for o in hull if "Skirts" in o.name]
        cols = [ucx("UCX_SM_Leopard2A8_00", body, tmp),
                ucx("UCX_SM_Leopard2A8_01", [o for o in turret if o.name in ("Turret_Body", "Turret_Armor")], tmp),
                ucx("UCX_SM_Leopard2A8_02", [], tmp, extra=[(x, y, z) for x in (2.9, MUZZLE_X)
                                                           for y in (-0.2, 0.2) for z in (GUN_Z - 0.2, GUN_Z + 0.2)])]
        select_only([sm] + cols)
        bpy.ops.export_scene.fbx(filepath=os.path.join(out, "SM_Leopard2A8.fbx"), object_types={"MESH"}, **FBX_COMMON)
        info["triangles"]["static_mesh_total"] = tri_count([sm])

        # Einzelnes Kettenglied (fuer Spline-/Instanz-basierte Ketten in UE5)
        link_tmpl.hide_viewport = False
        select_only([link_tmpl])
        bpy.ops.export_scene.fbx(filepath=os.path.join(out, "SM_Leopard2A8_TrackLink.fbx"),
                                 object_types={"MESH"}, **FBX_COMMON)
        link_tmpl.hide_viewport = True
        for o in list(tmp.objects):
            bpy.data.objects.remove(o)
        bpy.data.collections.remove(tmp)

        with open(os.path.join(out, "track_path_ue.json"), "w") as f:
            json.dump(track_info, f, indent=1)

    with open(os.path.join(out, "model_info.json"), "w") as f:
        json.dump({k: v for k, v in info.items() if k != "track"} |
                  {"track": {k: v for k, v in track_info.items() if k != "sides"}}, f, indent=2)
    print(json.dumps(info["dimensions_m"], indent=2))
    print(json.dumps(info["triangles"], indent=2))

    if not args.no_render:
        prev = render_views(os.path.abspath(args.renders), arm, args.samples, belts)
        prev.hide_render = False
    c_belt.hide_viewport = True
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out, "Leopard2A8.blend"), compress=True)
    bpy.ops.file.make_paths_relative()
    bpy.ops.wm.save_mainfile(compress=True)
    print("Fertig:", out)


if __name__ == "__main__":
    main()
