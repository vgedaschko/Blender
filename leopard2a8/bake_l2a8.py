"""
Textur-Bake fuer das Leopard-2A8-Modell (wird von build_leopard2a8.py mit --bake aufgerufen).

Je Texture-Set (Material) entstehen:
  T_L2A8_<Set>_BaseColor.png   sRGB  - Tarnlack, Kantenabrieb, Schmutz, Kennzeichnung
  T_L2A8_<Set>_Normal.png      DirectX-Normal-Map (UE5), Gruen invertiert
  T_L2A8_<Set>_Normal_GL.png   OpenGL-Normal-Map (Blender)
  T_L2A8_<Set>_ORM.png         R = Ambient Occlusion, G = Roughness, B = Metallic (UE5-Packing)

Ablauf je Set:
  1. Low-Poly = verbundene Kopie der Spielobjekte (UVs aus dem Build)
  2. High-Poly = Kopie + gebackene Schweissnaehte + prozedurale Oberflaechenstruktur (Bump)
  3. Bakes: Normal (Selected to Active), AO (mit restlichem Fahrzeug als Verdecker),
     BaseColor/Roughness/Metallic als Emission aus einem prozeduralen Shader
"""

import math
import os

import bpy  # muss vor bmesh importiert werden (pip-Modul)
import bmesh
import numpy as np
from mathutils import Matrix, Vector

SET_NAMES = {"hull": "Hull", "turret": "Turret", "gun": "Gun", "wheels": "RunningGear",
             "track": "Track", "track_belt": "TrackBelt"}

PAINT = {  # Tarnfarben (linear): NATO-Dreifarbtarnung
    "green": (0.040, 0.052, 0.028),
    "brown": (0.072, 0.042, 0.022),
    "black": (0.012, 0.012, 0.010),
}
STEEL_WORN = (0.32, 0.31, 0.29)
DUST = (0.20, 0.165, 0.115)


# =============================================================================
# Hilfen
# =============================================================================
def _sock(sockets, name, typ=None):
    for s in sockets:
        if s.name == name and (typ is None or s.type == typ) and s.enabled:
            return s
    for s in sockets:
        if s.name == name:
            return s
    raise KeyError(name)


class G:
    """Kleiner Node-Graph-Helfer."""

    def __init__(self, nt):
        self.nt = nt

    def n(self, typ, **props):
        node = self.nt.nodes.new(typ)
        for k, v in props.items():
            setattr(node, k, v)
        return node

    def link(self, a, b):
        self.nt.links.new(a, b)

    def math(self, op, a, b=None, clamp=False):
        m = self.n("ShaderNodeMath", operation=op, use_clamp=clamp)
        for i, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                m.inputs[i].default_value = v
            else:
                self.link(v, m.inputs[i])
        return m.outputs[0]

    def maprange(self, v, a, b, c=0.0, d=1.0):
        m = self.n("ShaderNodeMapRange", clamp=True)
        self.link(v, m.inputs["Value"])
        m.inputs["From Min"].default_value, m.inputs["From Max"].default_value = a, b
        m.inputs["To Min"].default_value, m.inputs["To Max"].default_value = c, d
        return m.outputs["Result"]

    def noise(self, vec, scale, detail=2.0, rough=0.5):
        m = self.n("ShaderNodeTexNoise")
        self.link(vec, m.inputs["Vector"])
        m.inputs["Scale"].default_value = scale
        m.inputs["Detail"].default_value = detail
        m.inputs["Roughness"].default_value = rough
        return m.outputs["Fac"]

    def mix(self, fac, a, b):
        m = self.n("ShaderNodeMix", data_type="RGBA", clamp_factor=True)
        for sock, v in ((_sock(m.inputs, "Factor", "VALUE"), fac), (_sock(m.inputs, "A", "RGBA"), a),
                        (_sock(m.inputs, "B", "RGBA"), b)):
            if isinstance(v, (tuple, list)):
                sock.default_value = (*v, 1.0) if len(v) == 3 else v
            elif isinstance(v, (int, float)):
                sock.default_value = v
            else:
                self.link(v, sock)
        return _sock(m.outputs, "Result", "RGBA")

    def mixf(self, fac, a, b):
        m = self.n("ShaderNodeMix", data_type="FLOAT", clamp_factor=True)
        for sock, v in ((_sock(m.inputs, "Factor", "VALUE"), fac), (_sock(m.inputs, "A", "VALUE"), a),
                        (_sock(m.inputs, "B", "VALUE"), b)):
            if isinstance(v, (int, float)):
                sock.default_value = v
            else:
                self.link(v, sock)
        return _sock(m.outputs, "Result", "VALUE")


def join_copies(objs, name, coll):
    copies = []
    for o in objs:
        c = o.copy()
        c.data = o.data.copy()
        c.parent = None
        c.matrix_world = o.matrix_world.copy()
        c.modifiers.clear()
        c.vertex_groups.clear()
        c.hide_render = c.hide_viewport = False
        coll.objects.link(c)
        copies.append(c)
    if len(copies) > 1:
        with bpy.context.temp_override(active_object=copies[0], selected_editable_objects=copies,
                                       selected_objects=copies, object=copies[0]):
            bpy.ops.object.join()
    j = copies[0]
    j.data.transform(j.matrix_world)
    j.matrix_world = Matrix.Identity(4)
    j.name = j.data.name = name
    return j


def select_only(objs, active):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active


def new_image(name, size, color=True):
    img = bpy.data.images.new(name, size, size, alpha=False)
    img.colorspace_settings.name = "sRGB" if color else "Non-Color"
    return img


def save_image(img, path):
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()


def dilate(img, iters=16, fill=(0.5, 0.5, 1.0)):
    """Unbelegte Pixel (Alpha 0) von den Inselraendern her auffuellen (Bake-Rand ohne Ueberschreiben)."""
    w, h = img.size
    a = img_array(img).reshape(h, w, 4)
    cov = a[..., 3] > 0.5
    rgb = a[..., :3].copy()
    for _ in range(iters):
        if cov.all():
            break
        acc = np.zeros_like(rgb)
        cnt = np.zeros(cov.shape, np.float32)
        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = np.roll(cov, d, (0, 1))
            acc += np.roll(rgb, d, (0, 1)) * c[..., None]
            cnt += c
        new = (~cov) & (cnt > 0)
        rgb[new] = acc[new] / cnt[new][:, None]
        cov |= new
    rgb[~cov] = fill
    img.pixels.foreach_set(np.concatenate([rgb, np.ones(cov.shape + (1,), np.float32)], -1).ravel())


def img_array(img):
    a = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
    img.pixels.foreach_get(a)
    return a.reshape(-1, 4)


# =============================================================================
# Schweissnaehte (nur High-Poly)
# =============================================================================
def weld_beads(objs, coll, r0=0.009, step=0.006):
    bm = bmesh.new()
    count = 0
    for o in objs:
        data = list(o.get("weld_edges", []))
        for i in range(0, len(data), 10):
            p1, p2 = Vector(data[i:i + 3]), Vector(data[i + 3:i + 6])
            n, convex = Vector(data[i + 6:i + 9]), data[i + 9] > 0.5
            d = p2 - p1
            L = d.length
            d.normalize()
            n = (n - d * n.dot(d)).normalized()
            b = d.cross(n)
            off = -0.004 if convex else 0.002
            steps = max(2, int(L / step))
            rings = []
            for k in range(steps + 1):
                t = k / steps
                r = r0 * (0.8 + 0.2 * abs(math.sin(math.pi * t * L / 0.010)))
                c = p1 + d * (t * L) + n * off
                rings.append([bm.verts.new(c + (n * math.cos(a) + b * math.sin(a)) * r)
                              for a in [j * math.pi / 3 for j in range(6)]])
            for A, B in zip(rings, rings[1:]):
                for j in range(6):
                    bm.faces.new((A[j], A[(j + 1) % 6], B[(j + 1) % 6], B[j]))
            count += 1
    me = bpy.data.meshes.new("BAKE_welds")
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new("BAKE_welds", me)
    coll.objects.link(o)
    return o, count


# =============================================================================
# Shader
# =============================================================================
def cross_mask(g, px, pz, ny, cx, cz, w, L):
    """Balkenkreuz (vereinfacht): Maske fuer Flaechen mit seitlicher Normale."""
    u = g.math("ABSOLUTE", g.math("SUBTRACT", px, cx))
    v = g.math("ABSOLUTE", g.math("SUBTRACT", pz, cz))
    a = g.math("MULTIPLY", g.math("LESS_THAN", u, w), g.math("LESS_THAN", v, L))
    b = g.math("MULTIPLY", g.math("LESS_THAN", v, w), g.math("LESS_THAN", u, L))
    side = g.math("GREATER_THAN", g.math("ABSOLUTE", ny), 0.6)
    return g.math("MULTIPLY", g.math("MAXIMUM", a, b), side)


def build_surface_shader(mat, group, local_ao=False):
    """Prozeduraler Lack-/Abrieb-/Schmutz-Shader. Liefert {pass: socket} fuer die Emission-Bakes."""
    if mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    g = G(nt)
    geo = g.n("ShaderNodeNewGeometry")
    pos = g.n("ShaderNodeSeparateXYZ")
    g.link(geo.outputs["Position"], pos.inputs[0])
    nrm = g.n("ShaderNodeSeparateXYZ")
    g.link(geo.outputs["Normal"], nrm.inputs[0])
    cid = g.n("ShaderNodeVertexColor", layer_name="ColorID")
    a_paint = g.n("ShaderNodeAttribute", attribute_name="paint_mask").outputs["Fac"]
    a_rough = g.n("ShaderNodeAttribute", attribute_name="roughness").outputs["Fac"]
    a_metal = g.n("ShaderNodeAttribute", attribute_name="metallic").outputs["Fac"]

    # Tarnlack (NATO-Dreifarb) mit leichter Farbstreuung
    ramp = g.n("ShaderNodeValToRGB")
    ramp.color_ramp.interpolation = "CONSTANT"
    el = ramp.color_ramp.elements
    el[0].position, el[0].color = 0.0, (*PAINT["black"], 1)
    el[1].position, el[1].color = 0.62, (*PAINT["brown"], 1)
    e = el.new(0.36)
    e.color = (*PAINT["green"], 1)
    g.link(g.noise(geo.outputs["Position"], 0.42, 1.5, 0.45), ramp.inputs["Fac"])
    var = g.maprange(g.noise(geo.outputs["Position"], 6.0, 3.0), 0.3, 0.7, 0.85, 1.15)
    tint = g.n("ShaderNodeMix", data_type="RGBA", blend_type="MULTIPLY", clamp_factor=True)
    _sock(tint.inputs, "Factor", "VALUE").default_value = 1.0
    g.link(ramp.outputs["Color"], _sock(tint.inputs, "A", "RGBA"))
    comb = g.n("ShaderNodeCombineXYZ")
    for i in range(3):
        g.link(var, comb.inputs[i])
    g.link(comb.outputs[0], _sock(tint.inputs, "B", "RGBA"))
    paint_col = _sock(tint.outputs, "Result", "RGBA")

    # Kantenabrieb: Abweichung der gerundeten (Bevel-)Normale von der echten Normale
    bev = g.n("ShaderNodeBevel", samples=8)
    bev.inputs["Radius"].default_value = 0.012
    dot = g.n("ShaderNodeVectorMath", operation="DOT_PRODUCT")
    g.link(bev.outputs["Normal"], dot.inputs[0])
    g.link(geo.outputs["Normal"], dot.inputs[1])
    edge = g.maprange(g.math("SUBTRACT", 1.0, dot.outputs["Value"]), 0.004, 0.05)
    breakup = g.maprange(g.noise(geo.outputs["Position"], 22.0, 6.0, 0.6), 0.42, 0.62)
    edge = g.math("MULTIPLY", edge, breakup, clamp=True)
    edge = g.math("MULTIPLY", edge, a_paint, clamp=True)

    # Schmutz: Hohlraeume (AO) + unten mehr, wolkig verteilt
    ao = g.n("ShaderNodeAmbientOcclusion", samples=16, only_local=local_ao)
    ao.inputs["Distance"].default_value = 0.5
    cav = g.math("SUBTRACT", 1.0, ao.outputs["AO"])
    low = g.maprange(pos.outputs["Z"], 0.25, 1.4, 1.0, 0.0)
    dirt = g.math("ADD", g.math("MULTIPLY", cav, 0.8), g.math("MULTIPLY", low, 0.4))
    dirt = g.math("MULTIPLY", dirt, g.maprange(g.noise(geo.outputs["Position"], 1.8, 3.0), 0.35, 0.65, 0.4, 1.0),
                  clamp=True)
    up = g.maprange(nrm.outputs["Z"], 0.6, 0.95, 0.0, 0.35)       # Staub auf waagerechten Flaechen
    dirt = g.math("ADD", dirt, g.math("MULTIPLY", up, g.noise(geo.outputs["Position"], 3.0)), clamp=True)

    base = g.mix(edge, paint_col, STEEL_WORN)
    base = g.mix(a_paint, cid.outputs["Color"], base)
    base = g.mix(g.math("MULTIPLY", dirt, 0.38), base, DUST)
    if group == "turret":   # Balkenkreuz auf den Turm-Seitenmodulen
        for s in (-1, 1):
            side = g.math("GREATER_THAN", g.math("MULTIPLY", pos.outputs["Y"], s), 1.30)
            outer = g.math("MULTIPLY", cross_mask(g, pos.outputs["X"], pos.outputs["Z"], nrm.outputs["Y"],
                                                  1.05, 2.16, 0.068, 0.195), side)
            inner = g.math("MULTIPLY", cross_mask(g, pos.outputs["X"], pos.outputs["Z"], nrm.outputs["Y"],
                                                  1.05, 2.16, 0.042, 0.17), side)
            base = g.mix(outer, base, (0.75, 0.75, 0.72))
            base = g.mix(inner, base, (0.012, 0.012, 0.012))

    rough = g.mixf(edge, a_rough, 0.35)
    rough = g.mixf(g.math("MULTIPLY", dirt, 0.6), rough, 0.92)
    metal = g.math("MAXIMUM", a_metal, g.math("MULTIPLY", edge, 0.9), clamp=True)

    emit = g.n("ShaderNodeEmission")
    out = g.n("ShaderNodeOutputMaterial")
    g.link(emit.outputs[0], out.inputs["Surface"])
    gray = {}
    for name, sock in (("rough", rough), ("metal", metal)):
        c = g.n("ShaderNodeCombineXYZ")
        for i in range(3):
            g.link(sock, c.inputs[i])
        gray[name] = c.outputs[0]
    img_node = g.n("ShaderNodeTexImage")
    nt.nodes.active = img_node
    return {"emit": emit, "img": img_node, "base": base, "rough": gray["rough"], "metal": gray["metal"], "g": g}


def high_material():
    """High-Poly: feine Walz-/Gussstruktur ueberall, Antirutschbelag auf waagerechten Lackflaechen."""
    mat = bpy.data.materials.get("BAKE_high") or bpy.data.materials.new("BAKE_high")
    if mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    g = G(nt)
    geo = g.n("ShaderNodeNewGeometry")
    nrm = g.n("ShaderNodeSeparateXYZ")
    g.link(geo.outputs["Normal"], nrm.inputs[0])
    paint = g.n("ShaderNodeAttribute", attribute_name="paint_mask").outputs["Fac"]
    fine = g.math("MULTIPLY", g.noise(geo.outputs["Position"], 35.0, 4.0, 0.6), 0.35)
    flat = g.math("MULTIPLY", g.maprange(nrm.outputs["Z"], 0.92, 0.98), paint)
    grit = g.math("MULTIPLY", g.noise(geo.outputs["Position"], 420.0, 1.0), flat)
    h = g.math("ADD", fine, grit)
    bump = g.n("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.35
    bump.inputs["Distance"].default_value = 0.0015
    g.link(h, bump.inputs["Height"])
    bsdf = g.n("ShaderNodeBsdfPrincipled")
    g.link(bump.outputs["Normal"], bsdf.inputs["Normal"])
    out = g.n("ShaderNodeOutputMaterial")
    g.link(bsdf.outputs[0], out.inputs["Surface"])
    return mat


# =============================================================================
# Bake
# =============================================================================
def _bake(kind, objs, active, img_node, img, selected_to_active=False, cage=0.03, dist=0.08, margin=8):
    img_node.image = img
    select_only(objs, active)
    bpy.ops.object.bake(type=kind, use_selected_to_active=selected_to_active, cage_extrusion=cage,
                        max_ray_distance=dist, margin=margin, use_clear=True, target="IMAGE_TEXTURES")


def _emit_passes(sh, objs, active, prefix, size, selected_to_active=False, cage=0.03, dist=0.08):
    imgs = {}
    for key, color in (("base", True), ("rough", False), ("metal", False)):
        sh["g"].link(sh[key], sh["emit"].inputs["Color"])
        img = new_image(f"{prefix}_{key}", size, color)
        _bake("EMIT", objs, active, sh["img"], img, selected_to_active, cage, dist)
        imgs[key] = img
    return imgs


def _write_set(name, imgs, out_dir):
    """BaseColor, Normal (DX + GL) und ORM schreiben; liefert Pfade."""
    os.makedirs(out_dir, exist_ok=True)
    p = {k: os.path.join(out_dir, f"T_L2A8_{name}_{k}.png") for k in ("BaseColor", "Normal", "Normal_GL", "ORM")}
    save_image(imgs["base"], p["BaseColor"])
    save_image(imgs["normal"], p["Normal_GL"])
    size = imgs["normal"].size[0]
    nrm = img_array(imgs["normal"])
    dx = nrm.copy()
    dx[:, 1] = 1.0 - dx[:, 1]
    out = new_image(f"T_{name}_dx", size, False)
    out.pixels.foreach_set(dx.ravel())
    save_image(out, p["Normal"])
    ao = img_array(imgs["ao"])[:, 0] if "ao" in imgs else np.ones(size * size, np.float32)
    orm = np.stack([ao, img_array(imgs["rough"])[:, 0], img_array(imgs["metal"])[:, 0],
                    np.ones_like(ao)], axis=1)
    out2 = new_image(f"T_{name}_orm", size, False)
    out2.pixels.foreach_set(orm.ravel())
    save_image(out2, p["ORM"])
    for im in list(imgs.values()) + [out, out2]:
        bpy.data.images.remove(im)
    return p


def bake_set(group, objs, out_dir, size, samples, chain_pitch=None):
    """Baked ein Texture-Set aus den Objekten `objs`; das restliche sichtbare Fahrzeug wirkt als Verdecker.
    chain_pitch: einzelnes Kettenglied abseits des Fahrzeugs mit Nachbargliedern backen."""
    coll = bpy.data.collections.get("L2A8_Bake") or bpy.data.collections.new("L2A8_Bake")
    if coll.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(coll)
    sc = bpy.context.scene
    name = SET_NAMES[group]

    # Ausgangsobjekte ausblenden (Low-/High-Kopien liegen deckungsgleich darauf)
    hidden = [o for o in objs if not o.hide_render]
    for o in hidden:
        o.hide_render = True
    low = join_copies(objs, f"BAKE_low_{group}", coll)
    extra = []
    if chain_pitch:
        off = Matrix.Translation((0, 0, -50))
        low.data.transform(off)
        for k in (-1, 1):
            nb = low.copy()
            nb.data = low.data.copy()
            nb.data.transform(Matrix.Translation((k * chain_pitch, 0, 0)))
            coll.objects.link(nb)
            extra.append(nb)

    bake_mat = bpy.data.materials.new(f"BAKE_{group}")
    low.data.materials.clear()
    low.data.materials.append(bake_mat)
    sh = build_surface_shader(bake_mat, group, local_ao=(group == "track"))
    imgs = {}

    # Normal: High -> Low pro Objekt (verhindert Treffer auf fremde, ueberlappende Teile)
    sc.cycles.samples = 4
    nimg = bpy.data.images.new(f"{name}_normal", size, size, alpha=True)
    nimg.colorspace_settings.name = "Non-Color"
    nimg.pixels.foreach_set(np.zeros(size * size * 4, np.float32))
    low.hide_render = True
    n_beads = 0
    hmat = high_material()
    for o in objs:
        lo = join_copies([o], "BAKE_lo", coll)
        hi = join_copies([o], "BAKE_hi", coll)
        if "weld_edges" in o:
            beads, nb = weld_beads([o], coll)
            n_beads += nb
            with bpy.context.temp_override(active_object=hi, selected_editable_objects=[hi, beads],
                                           selected_objects=[hi, beads], object=hi):
                bpy.ops.object.join()
        lo.data.materials.clear()
        lo.data.materials.append(bake_mat)
        hi.data.materials.clear()
        hi.data.materials.append(hmat)
        sh["img"].image = nimg
        select_only([hi, lo], lo)
        # kleiner Kafig: High = Low + Schweissnaehte (ragen nur ~1 cm heraus), Nachbarteile nicht treffen
        bpy.ops.object.bake(type="NORMAL", use_selected_to_active=True, cage_extrusion=0.015,
                            max_ray_distance=0.04, margin=0, use_clear=False, target="IMAGE_TEXTURES")
        bpy.data.objects.remove(lo)
        bpy.data.objects.remove(hi)
    dilate(nimg)
    imgs["normal"] = nimg
    low.hide_render = False

    # AO auf dem Low-Poly, restliches Fahrzeug verdeckt mit
    sc.cycles.samples = samples
    imgs["ao"] = new_image(f"{name}_ao", size, False)
    _bake("AO", [low], low, sh["img"], imgs["ao"])

    # BaseColor / Roughness / Metallic
    sc.cycles.samples = max(8, samples // 4)
    imgs.update(_emit_passes(sh, [low], low, name, size))

    paths = _write_set(name, imgs, out_dir)
    for o in [low] + extra:
        bpy.data.objects.remove(o)
    bpy.data.materials.remove(bake_mat)
    for o in hidden:
        o.hide_render = False
    print(f"Bake {name}: {size}px, {n_beads} Schweissnaehte")
    return paths


def bake_belt_tile(link_tmpl, pitch, sec, ucum, out_dir, size, samples):
    """Kachel fuer das Kettenband: ein Kettenglied (plus Nachbarn) auf ein gerades Bandstueck backen.
    1 Kachel in V = 1 Kettenglied, U = Querschnitt wie im Band."""
    coll = bpy.data.collections.get("L2A8_Bake")
    sc = bpy.context.scene
    bm = bmesh.new()
    uv = bm.loops.layers.uv.new("UVMap")
    rings = []
    for x in (-pitch / 2, pitch / 2):
        rings.append([bm.verts.new((x, y, -n)) for n, y in sec])   # wie untere Bandseite: aussen = -Z
    for i in range(len(sec)):
        j = (i + 1) % len(sec)
        A, B = rings
        f = bm.faces.new((A[i], A[j], B[j], B[i]))
        for loop, (u, v) in zip(f.loops, ((ucum[i], 0), (ucum[i + 1], 0), (ucum[i + 1], 1), (ucum[i], 1))):
            loop[uv].uv = (u, v)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new("BAKE_belt_tile")
    bm.to_mesh(me)
    bm.free()
    low = bpy.data.objects.new("BAKE_belt_tile", me)
    coll.objects.link(low)

    links = []
    for k in (-1, 0, 1):
        o = link_tmpl.copy()
        o.data = link_tmpl.data.copy()
        o.modifiers.clear()
        X, Z = Vector((1, 0, 0)), Vector((0, 0, -1))
        m = Matrix((X, Z.cross(X), Z)).transposed().to_4x4()
        m.translation = (k * pitch, 0, 0)
        o.matrix_world = m
        o.hide_render = o.hide_viewport = False
        coll.objects.link(o)
        links.append(o)
    high = links[1]
    with bpy.context.temp_override(active_object=high, selected_editable_objects=links,
                                   selected_objects=links, object=high):
        bpy.ops.object.join()
    hmat = bpy.data.materials.new("BAKE_belt_high")
    high.data.materials.clear()
    high.data.materials.append(hmat)
    sh = build_surface_shader(hmat, "track", local_ao=True)
    lmat = bpy.data.materials.new("BAKE_belt_low")
    low.data.materials.append(lmat)
    lmat.use_nodes = True
    limg = lmat.node_tree.nodes.new("ShaderNodeTexImage")
    lmat.node_tree.nodes.active = limg

    imgs = {}
    sc.cycles.samples = 4
    imgs["normal"] = new_image("belt_normal", size, False)
    _bake("NORMAL", [high, low], low, limg, imgs["normal"], True, cage=0.06, dist=0.25)
    sc.cycles.samples = max(8, samples // 4)
    for key, color in (("base", True), ("rough", False), ("metal", False)):
        sh["g"].link(sh[key], sh["emit"].inputs["Color"])
        img = new_image(f"belt_{key}", size, color)
        _bake("EMIT", [high, low], low, limg, img, True, cage=0.06, dist=0.25)
        imgs[key] = img
    paths = _write_set("TrackBelt", imgs, out_dir)
    for o in (low, high):
        bpy.data.objects.remove(o)
    for m in (hmat, lmat):
        bpy.data.materials.remove(m)
    print(f"Bake TrackBelt: {size}px")
    return paths


# =============================================================================
# Endgueltige Materialien
# =============================================================================
def textured_material(mat, paths):
    """Ersetzt den Vorschau-Shader durch BaseColor/ORM/Normal-Texturen (exportiert ins FBX)."""
    nt = mat.node_tree
    nt.nodes.clear()
    g = G(nt)

    def tex(path, color):
        img = bpy.data.images.load(path, check_existing=True)
        img.colorspace_settings.name = "sRGB" if color else "Non-Color"
        return g.n("ShaderNodeTexImage", image=img)
    bsdf = g.n("ShaderNodeBsdfPrincipled")
    out = g.n("ShaderNodeOutputMaterial")
    g.link(bsdf.outputs[0], out.inputs["Surface"])
    base = tex(paths["BaseColor"], True)
    orm = tex(paths["ORM"], False)
    sep = g.n("ShaderNodeSeparateColor")
    g.link(orm.outputs["Color"], sep.inputs[0])
    # AO dezent in die Grundfarbe (Blender kennt keinen AO-Eingang; UE5 nutzt den ORM-R-Kanal)
    aomul = g.n("ShaderNodeMix", data_type="RGBA", blend_type="MULTIPLY", clamp_factor=True)
    _sock(aomul.inputs, "Factor", "VALUE").default_value = 0.6
    g.link(base.outputs["Color"], _sock(aomul.inputs, "A", "RGBA"))
    g.link(sep.outputs[0], _sock(aomul.inputs, "B", "RGBA"))
    g.link(_sock(aomul.outputs, "Result", "RGBA"), bsdf.inputs["Base Color"])
    g.link(sep.outputs[1], bsdf.inputs["Roughness"])
    g.link(sep.outputs[2], bsdf.inputs["Metallic"])
    nrm = tex(paths["Normal_GL"], False)
    nmap = g.n("ShaderNodeNormalMap")
    g.link(nrm.outputs["Color"], nmap.inputs["Color"])
    g.link(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    mat["textures"] = {k: os.path.basename(v) for k, v in paths.items()}
