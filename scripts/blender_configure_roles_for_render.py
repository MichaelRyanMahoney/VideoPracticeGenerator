import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bpy

TRACE = False

# Hard-coded top colors per role (sRGB hex), per request
TOP_COLOR_HEX_BY_ROLE = {
    "Disputant1": "01baef",
    "MediatorA":  "030027",
    "MediatorB":  "fdb92a",
    "Disputant2": "db504a",
}

def parse_args(default_base: Path) -> dict:
    argv = sys.argv
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    opts = {
        "config": str(default_base / "manifests" / "generator_inputs.json"),
        "scene": None,
        "save": False,
        "save_as": None,
        "dry_run": False,
        "trace": False,
        "hdri_path": None,
        "hdri_strength": None,
        "hdri_from_config": None,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--config" and i + 1 < len(args):
            opts["config"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        if a == "--scene" and i + 1 < len(args):
            opts["scene"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        if a == "--save":
            opts["save"] = True
            i += 1
            continue
        if a == "--save-as" and i + 1 < len(args):
            opts["save_as"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        if a == "--dry-run":
            opts["dry_run"] = True
            i += 1
            continue
        if a == "--trace":
            opts["trace"] = True
            i += 1
            continue
        if a == "--hdri_path" and i + 1 < len(args):
            opts["hdri_path"] = str(Path(args[i + 1]).expanduser())
            i += 2
            continue
        if a == "--hdri_strength" and i + 1 < len(args):
            try:
                opts["hdri_strength"] = float(args[i + 1])
            except Exception:
                opts["hdri_strength"] = None
            i += 2
            continue
        if a == "--hdri_from_config" and i + 1 < len(args):
            opts["hdri_from_config"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        i += 1
    return opts


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def role_prefix_map(cfg: dict) -> Dict[str, str]:
    return cfg.get("blender_mapping", {}).get("role_prefix", {})


def try_resolve_missing_files(search_dir: Path) -> None:
    """Resolve missing external images (e.g., HDRI) after the scene has been copied to a new location."""
    try:
        import bpy
        # Try Blender's built-in resolver over the project root
        bpy.ops.file.find_missing_files(directory=str(search_dir), recursive=True)
        if TRACE:
            print(f"[TRACE] find_missing_files in: {search_dir}")
    except Exception:
        # Fallback: try to make paths absolute based on search_dir
        try:
            for img in list(bpy.data.images):
                fp = getattr(img, "filepath", "") or ""
                if not fp:
                    continue
                p = Path(fp)
                if p.exists():
                    continue
                # If path is relative to the copied .blend, try resolving from project root
                candidate = (search_dir / p.name)
                if candidate.exists():
                    try:
                        img.filepath = str(candidate)
                        if TRACE:
                            print(f"[TRACE] Remapped image '{img.name}' -> {candidate}")
                    except Exception:
                        pass
        except Exception:
            pass


def apply_hdri_environment(hdri_path: Path, strength: float = 0.7) -> None:
    """
    Force the World to use the provided HDRI at the given strength.
    Creates/updates nodes:
      Environment Texture -> Background -> World Output
    """
    try:
        import bpy
        world = bpy.context.scene.world
        if not world:
            world = bpy.data.worlds.new("World")
            bpy.context.scene.world = world
        world.use_nodes = True
        nt = world.node_tree
        nodes = nt.nodes
        links = nt.links
        # Get/create nodes
        bg = None
        out = None
        env = None
        for n in nodes:
            t = getattr(n, "type", "")
            if t == "BACKGROUND" and bg is None:
                bg = n
            elif t == "OUTPUT_WORLD" and out is None:
                out = n
            elif t == "TEX_ENVIRONMENT" and env is None:
                env = n
        if bg is None:
            bg = nodes.new("ShaderNodeBackground")
        if out is None:
            out = nodes.new("ShaderNodeOutputWorld")
        if env is None:
            env = nodes.new("ShaderNodeTexEnvironment")
        # Load image
        img = bpy.data.images.load(str(hdri_path), check_existing=True)
        try:
            img.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
        env.image = img
        # Position nodes (cosmetic)
        try:
            env.location = (-600, 0)
            bg.location = (-300, 0)
            out.location = (0, 0)
        except Exception:
            pass
        # Connect nodes
        def link(a, a_sock, b, b_sock):
            try:
                links.new(a.outputs[a_sock], b.inputs[b_sock])
            except Exception:
                pass
        # Clear existing links to World Output
        try:
            for l in list(links):
                if l.to_node == out:
                    links.remove(l)
        except Exception:
            pass
        # Ensure env -> bg -> out
        link(env, "Color", bg, "Color")
        try:
            bg.inputs["Strength"].default_value = float(strength)
        except Exception:
            pass
        link(bg, "Background", out, "Surface")
        if TRACE:
            print(f"[TRACE] Applied HDRI: {hdri_path} (strength={strength})")
    except Exception as ex:
        print(f"[WARN] Failed to apply HDRI '{hdri_path}': {ex}")


def ensure_scene(scene_path: Optional[str]) -> None:
    if scene_path:
        bpy.ops.wm.open_mainfile(filepath=scene_path)

def set_collections_visible_for_render(root_col: bpy.types.Collection) -> None:
    """Ensure the given collection tree is visible for both viewport and render, across all view layers."""
    if not root_col:
        return
    # Set flags on the data collections
    def mark(col: bpy.types.Collection):
        try:
            col.hide_render = False
        except Exception:
            pass
        try:
            col.hide_viewport = False
        except Exception:
            pass
        for c in col.children:
            mark(c)
    mark(root_col)

    # Clear excludes in all view layers
    def reveal_in_layer(layer_col, target: bpy.types.Collection):
        if layer_col.collection == target:
            try:
                layer_col.exclude = False
                layer_col.holdout = False
                layer_col.indirect_only = False
            except Exception:
                pass
        for ch in layer_col.children:
            reveal_in_layer(ch, target)

    for vl in bpy.context.scene.view_layers:
        # Walk entire subtree and reveal each collection node in layer tree
        def walk_and_reveal(col: bpy.types.Collection):
            reveal_in_layer(vl.layer_collection, col)
            for ch in col.children:
                walk_and_reveal(ch)
        walk_and_reveal(root_col)


def iter_collection_objects(col: bpy.types.Collection):
    for o in col.objects:
        yield o
    for c in col.children:
        yield from iter_collection_objects(c)


def set_all_hidden(col: bpy.types.Collection, dry: bool) -> None:
    for obj in iter_collection_objects(col):
        if TRACE:
            print(f"[TRACE] hide_render True: {obj.name}")
        if not dry:
            obj.hide_render = True
            # Also hide in viewport and disable in viewports
            try:
                obj.hide_viewport = True
            except Exception:
                pass
            try:
                obj.hide_set(True)
            except Exception:
                pass


def find_objects_by_prefix(col: bpy.types.Collection, prefix: str) -> Dict[str, bpy.types.Object]:
    result = {}
    for obj in iter_collection_objects(col):
        result[obj.name] = obj
    return result

def _find_collection_in_subtree(root: bpy.types.Collection, name_base: str) -> Optional[bpy.types.Collection]:
    """Find a collection whose name equals name_base or name_base.### within root's subtree."""
    name_l = name_base.lower()
    def match(n: str) -> bool:
        nl = n.lower()
        return nl == name_l or nl.startswith(name_l + ".")
    def walk(c: bpy.types.Collection):
        if match(c.name):
            return c
        for ch in c.children:
            hit = walk(ch)
            if hit:
                return hit
        return None
    return walk(root)

def set_collection_visible_recursive(coll: Optional[bpy.types.Collection], dry: bool) -> None:
    if not coll:
        return
    for o in iter_collection_objects(coll):
        set_visible(o, dry)

def _first_object_with_materials(coll: Optional[bpy.types.Collection]) -> Optional[bpy.types.Object]:
    if not coll:
        return None
    for o in iter_collection_objects(coll):
        try:
            if getattr(o, "material_slots", None) and len(o.material_slots) > 0:
                return o
        except Exception:
            pass
    # fallback: any object
    for o in iter_collection_objects(coll):
        return o
    return None


def pick_best_match(name_base: str, objects_by_name: Dict[str, bpy.types.Object]) -> Optional[bpy.types.Object]:
    # Prefer exact, then .001, .002... with the lowest numeric suffix
    if name_base in objects_by_name:
        return objects_by_name[name_base]
    candidates: List[Tuple[int, str]] = []
    dot = name_base + "."
    for n in objects_by_name.keys():
        if n.startswith(dot):
            try:
                num = int(n.split(".")[-1])
            except Exception:
                continue
            candidates.append((num, n))
    if not candidates:
        return None
    candidates.sort()
    return objects_by_name[candidates[0][1]]


def set_visible(obj: Optional[bpy.types.Object], dry: bool) -> None:
    if not obj:
        return
    if TRACE:
        print(f"[TRACE] hide_render False: {obj.name}")
    if not dry:
        obj.hide_render = False
        # Make visible in viewport
        try:
            obj.hide_viewport = False
        except Exception:
            pass
        try:
            obj.hide_set(False)
        except Exception:
            pass


def hex_to_rgba(hex_str: str) -> Tuple[float, float, float, float]:
    s = hex_str.strip().lstrip("#")
    if len(s) != 6:
        return (1.0, 1.0, 1.0, 1.0)
    def srgb_to_linear(c: float) -> float:
        # Convert sRGB [0..1] → linear
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r_s = int(s[0:2], 16) / 255.0
    g_s = int(s[2:4], 16) / 255.0
    b_s = int(s[4:6], 16) / 255.0
    r = srgb_to_linear(r_s)
    g = srgb_to_linear(g_s)
    b = srgb_to_linear(b_s)
    return (r, g, b, 1.0)


def set_body_color(body_obj: Optional[bpy.types.Object], hex_color: str, dry: bool, prefix: str = "") -> None:
    if not body_obj or not body_obj.data:
        return
    rgba = hex_to_rgba(hex_color)
    if dry or TRACE:
        print(f"[DRY] Set body color {rgba} on {body_obj.name}")
        if dry:
            return
    # Prefer the specific body skin material if present
    target_names = (f"{prefix}mat_boy_girl_skin", "mat_boy_girl_skin")
    # First pass: match by name startswith
    for slot in body_obj.material_slots:
        mat = slot.material
        if not mat:
            continue
        if any(mat.name.startswith(n) for n in target_names):
            if apply_rgba_to_material(mat, rgba):
                return
    # Fallback: first Principled in any slot
    for slot in body_obj.material_slots:
        mat = slot.material
        if not mat:
            continue
        if apply_rgba_to_material(mat, rgba):
            return


def set_object_material_color(obj: Optional[bpy.types.Object], base_names: Tuple[str, ...], hex_color: str, dry: bool, prefix: str = "") -> None:
    if not obj:
        return
    rgba = hex_to_rgba(hex_color)
    if dry and not TRACE:
        return
    # Try to find a material on this object that starts with any of base_names
    names_with_prefix = tuple(f"{prefix}{n}" for n in base_names)
    for slot in obj.material_slots:
        mat = slot.material
        if not mat:
            continue
        lname = mat.name.lower()
        candidates = tuple(n.lower() for n in (names_with_prefix + base_names))
        if any(lname.startswith(n) for n in candidates):
            # Ensure nodes exist
            if hasattr(mat, "use_nodes"):
                mat.use_nodes = True
            if apply_rgba_to_material(mat, rgba):
                if dry:
                    print(f"[DRY] Set material '{mat.name}' color {rgba} on {obj.name}")
                return
    # Fallback: first principled anywhere
    for slot in obj.material_slots:
        mat = slot.material
        if not mat:
            continue
        if hasattr(mat, "use_nodes"):
            mat.use_nodes = True
        if mat.node_tree:
            if apply_rgba_to_material(mat, rgba):
                if dry:
                    print(f"[DRY] Set first Principled material '{mat.name}' color {rgba} on {obj.name}")
                return
        try:
            mat.diffuse_color = rgba
            if dry:
                print(f"[DRY] Set diffuse color on '{mat.name}' to {rgba} for {obj.name}")
            return
        except Exception:
            pass

def set_object_all_principled_color(obj: Optional[bpy.types.Object], hex_color: str, dry: bool) -> None:
    """Apply color to all Principled BSDF nodes on all materials of an object."""
    if not obj:
        return
    rgba = hex_to_rgba(hex_color)
    for idx, slot in enumerate(obj.material_slots):
        mat = slot.material
        if not mat:
            continue
        if dry:
            print(f"[DRY] Set top color {rgba} on material '{mat.name}' (slot {idx}) for {obj.name}")
            # still run setter in dry to verify sockets exist (no write will occur inside apply helper)
        apply_rgba_to_material(mat, rgba)

def apply_rgba_to_material(mat: bpy.types.Material, rgba: Tuple[float, float, float, float]) -> bool:
    """Set color robustly on a material: disconnect inputs and set Base Color/Color."""
    if not mat:
        return False
    try:
        mat.use_nodes = True
    except Exception:
        pass
    nt = getattr(mat, "node_tree", None)
    if not nt:
        try:
            mat.diffuse_color = rgba
            return True
        except Exception:
            return False

    def set_on_socket(sock):
        try:
            if sock.is_linked:
                for link in list(nt.links):
                    if link.to_socket == sock:
                        nt.links.remove(link)
        except Exception:
            pass
        try:
            sock.default_value = rgba
            return True
        except Exception:
            return False

    # Principled BSDF
    changed_any = False
    for node in nt.nodes:
        if getattr(node, "bl_idname", "") == "ShaderNodeBsdfPrincipled" or node.type == "BSDF_PRINCIPLED":
            base = node.inputs.get("Base Color")
            if base and set_on_socket(base):
                changed_any = True
            subsurf = node.inputs.get("Subsurface Color")
            if subsurf and set_on_socket(subsurf):
                changed_any = True
    if changed_any:
        return True

    # Hair BSDF / Principled Hair
    for node in nt.nodes:
        if getattr(node, "bl_idname", "") in ("ShaderNodeBsdfHair", "ShaderNodeBsdfHairPrincipled") or node.type in ("BSDF_HAIR",):
            sock = node.inputs.get("Color")
            if sock and set_on_socket(sock):
                return True

    # Any node with Base Color or Color
    for node in nt.nodes:
        for key in ("Base Color", "Color"):
            sock = node.inputs.get(key)
            if sock and set_on_socket(sock):
                return True

    # RGB node fallback
    for node in nt.nodes:
        if getattr(node, "bl_idname", "") == "ShaderNodeRGB" or node.type == "RGB":
            try:
                node.outputs[0].default_value = rgba
                return True
            except Exception:
                pass

    try:
        mat.diffuse_color = rgba
        return True
    except Exception:
        return False


def ensure_unique_materials(obj: Optional[bpy.types.Object], name_prefixes: Tuple[str, ...], dry: bool) -> None:
    if not obj:
        return
    for i, slot in enumerate(obj.material_slots):
        mat = slot.material
        if not mat:
            continue
        if any(mat.name.startswith(p) for p in name_prefixes):
            if dry:
                print(f"[DRY] Would make single-user material for '{mat.name}' on {obj.name}")
                continue
            # Duplicate material so each character can have independent color
            new_mat = mat.copy()
            slot.material = new_mat

def find_material_by_base(prefix: str, base_name: str) -> Optional[bpy.types.Material]:
    """Find a material by exact name or by the lowest suffix match, considering prefixed and unprefixed forms."""
    candidates = []
    exact_names = [f"{prefix}{base_name}", base_name]
    for name in exact_names:
        mat = bpy.data.materials.get(name)
        if mat:
            return mat
    # Search by startswith and pick lowest numeric suffix
    for mat in bpy.data.materials:
        n = mat.name
        if n.startswith(f"{prefix}{base_name}.") or n.startswith(f"{base_name}."):
            try:
                suffix = int(n.split(".")[-1])
            except Exception:
                continue
            candidates.append((suffix, mat))
    if candidates:
        candidates.sort()
        return candidates[0][1]
    return None

def iter_slots_matching_base(obj: Optional[bpy.types.Object], prefix: str, base_name: str):
    if not obj:
        return
    targets = (f"{prefix}{base_name}", base_name)
    targets_l = tuple(t.lower() for t in targets)
    for i, slot in enumerate(obj.material_slots):
        mat = slot.material
        if not mat:
            continue
        name = mat.name
        lname = name.lower()
        if any(lname == t or lname.startswith(f"{t}.") for t in targets_l):
            yield i, slot

def debug_print_body_materials(body: Optional[bpy.types.Object], dry: bool) -> None:
    if (not dry and not TRACE) or not body:
        return
    print(f"[DRY] Body object: {body.name} has {len(body.material_slots)} material slot(s):")
    for idx, slot in enumerate(body.material_slots):
        mat = slot.material
        print(f"[DRY]   slot[{idx}]: {(mat.name if mat else 'None')}")

def debug_print_object_materials(obj: Optional[bpy.types.Object], title: str) -> None:
    if not TRACE or not obj:
        return
    print(f"[TRACE] {title}: {obj.name} materials ({len(obj.material_slots)})")
    for idx, slot in enumerate(obj.material_slots):
        mat = slot.material
        mname = mat.name if mat else "None"
        print(f"[TRACE]   slot[{idx}] -> {mname}")
        if mat and getattr(mat, "node_tree", None):
            for node in mat.node_tree.nodes:
                t = getattr(node, "bl_idname", node.type)
                print(f"[TRACE]     node: {t}")
                for inp in node.inputs:
                    try:
                        linked = inp.is_linked
                    except Exception:
                        linked = False
                    print(f"[TRACE]       input '{inp.name}' linked={linked}")

def get_or_create_simple_skin_material(name: str, rgba: Tuple[float, float, float, float]) -> bpy.types.Material:
    mat = bpy.data.materials.get(name)
    if not mat:
        mat = bpy.data.materials.new(name=name)
        mat.use_nodes = True
        # Clear default nodes
        nt = mat.node_tree
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        # Create Principled and Output
        principled = nt.nodes.new("ShaderNodeBsdfPrincipled")
        principled.location = (0, 0)
        output = nt.nodes.new("ShaderNodeOutputMaterial")
        output.location = (200, 0)
        nt.links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    # Set color on Principled
    try:
        mat.use_nodes = True
        nt = mat.node_tree
        for node in nt.nodes:
            if node.type == "BSDF_PRINCIPLED":
                node.inputs["Base Color"].default_value = rgba
                node.inputs["Subsurface Color"].default_value = rgba
                break
    except Exception:
        try:
            mat.diffuse_color = rgba
        except Exception:
            pass
    return mat

def configure_role(role: str, cfg: dict, dry: bool) -> None:
    rp = role_prefix_map(cfg)
    prefix = rp.get(role, "")
    char = cfg["characters"][role]
    gender = (char.get("blender", {}).get("gender", "M") or "M").upper()
    skin_hex = char.get("blender", {}).get("skin_hex", "FFFFFF")
    hair_hex = char.get("blender", {}).get("hair_hex", "2B2B2B")
    selectors = char.get("blender", {}).get("selectors", {})

    root_col = bpy.data.collections.get(role)
    if not root_col:
        print(f"[WARN] Role collection '{role}' not found in scene; skipping.")
        return

    # Make sure the whole role collection tree is visible for render/viewport
    set_collections_visible_for_render(root_col)

    # Hide all then re-enable chosen
    set_all_hidden(root_col, dry)
    objs_by_name = find_objects_by_prefix(root_col, prefix)

    # Core objects by gender
    gender_key = "girl" if gender == "F" else "boy"
    eyes_base = f"{prefix}geo_{gender_key}_eyes"
    nose_base = f"{prefix}geo_{gender_key}_nose"
    # Prefer geo_body.001 over geo_body
    body_base_primary = f"{prefix}geo_body.001"
    body_base_fallback = f"{prefix}geo_body"
    # Prefer geo_teeth.001 over geo_teeth
    teeth_base_primary = f"{prefix}geo_teeth.001"
    teeth_base_fallback = f"{prefix}geo_teeth"

    eyes = pick_best_match(eyes_base, objs_by_name)
    nose = pick_best_match(nose_base, objs_by_name)
    body = pick_best_match(body_base_primary, objs_by_name) or pick_best_match(body_base_fallback, objs_by_name)
    teeth = pick_best_match(teeth_base_primary, objs_by_name) or pick_best_match(teeth_base_fallback, objs_by_name)

    # Also show hair basement helper object: "{prefix}0. hair_basement - {gender_key}"
    basement_base = f"{prefix}0. hair_basement - {gender_key}"
    basement = pick_best_match(basement_base, objs_by_name)

    set_visible(eyes, dry)
    set_visible(nose, dry)
    set_visible(body, dry)
    set_visible(teeth, dry)
    set_visible(basement, dry)

    # Explicitly ensure opposite-gender eyes/nose remain hidden (defensive)
    try:
        other_gender = "boy" if gender_key == "girl" else "girl"
        other_eyes = f"{prefix}geo_{other_gender}_eyes".lower()
        other_nose = f"{prefix}geo_{other_gender}_nose".lower()
        for name, obj in list(objs_by_name.items()):
            ln = name.lower()
            if ln.startswith(other_eyes) or ln.startswith(other_nose):
                if TRACE:
                    print(f"[TRACE] ensure hidden: {name}")
                if not dry:
                    try:
                        obj.hide_render = True
                        obj.hide_set(True)
                    except Exception:
                        pass
    except Exception:
        pass

    # Outfit: hair, shirt, pants with prefix applied
    hair_obj = None
    shirt_obj = None
    for key in ("hair", "shirt", "pants"):
        base = selectors.get(key)
        if not base:
            continue
        target = f"{prefix}{base}"
        obj = pick_best_match(target, objs_by_name)
        if obj:
            set_visible(obj, dry)
            if key == "hair":
                hair_obj = obj
            elif key == "shirt":
                shirt_obj = obj
        else:
            # Try collection match if object not found
            coll = _find_collection_in_subtree(root_col, target)
            if coll:
                set_collection_visible_recursive(coll, dry)
                if key == "hair" and hair_obj is None:
                    hair_obj = _first_object_with_materials(coll)
                if key == "shirt" and shirt_obj is None:
                    shirt_obj = _first_object_with_materials(coll)
            else:
                if TRACE or dry:
                    print(f"[WARN] Selector '{key}' target not found: {target}")
    debug_print_object_materials(hair_obj, f"{role} hair")

    # Apply body color
    # Color all slots on the chosen body that use the skin material base; do not duplicate or reassign materials
    rgba = hex_to_rgba(skin_hex)
    any_changed = False
    debug_print_body_materials(body, dry)
    debug_print_object_materials(body, f"{role} body")
    for _i, slot in iter_slots_matching_base(body, prefix, "mat_boy_girl_skin"):
        mat = slot.material
        if not mat:
            continue
        if dry:
            print(f"[DRY] Set skin material color {rgba} on '{mat.name}' (object {body.name})")
        if apply_rgba_to_material(mat, rgba):
            any_changed = True
    if not any_changed:
        # Fallback to previous strategy (first matching or first principled) without duplication
        if dry or TRACE:
            print(f"[DRY] No direct skin slot recolor matched; falling back to set_body_color({rgba}) for {body.name}")
        set_body_color(body, skin_hex, dry, prefix=prefix)
    # Apply hair color (materials named like mat_girl_hair or mat_boy_hair with possible .001)
    ensure_unique_materials(hair_obj, ("mat_girl_hair", "mat_boy_hair"), dry)
    set_object_material_color(hair_obj, ("mat_girl_hair", "mat_boy_hair"), hair_hex, dry, prefix=prefix)

    # Apply hard-coded top color by role
    top_hex = TOP_COLOR_HEX_BY_ROLE.get(role)
    if top_hex:
        set_object_all_principled_color(shirt_obj, top_hex, dry)


def main():
    base_dir = Path(__file__).resolve().parents[1]
    opts = parse_args(base_dir)
    global TRACE
    TRACE = bool(opts.get("trace"))
    cfg = load_config(opts["config"])

    ensure_scene(opts["scene"])
    # World/HDRI setup from config (preferred), else attempt to resolve missing files
    try:
        # Priority: explicit CLI -> external config file -> generator_inputs.json run section
        hdri_path_raw = opts.get("hdri_path")
        hdri_strength = opts.get("hdri_strength")
        if not hdri_path_raw and opts.get("hdri_from_config"):
            try:
                with open(opts["hdri_from_config"], "r", encoding="utf-8") as f:
                    _hc = json.load(f)
                hdri_path_raw = _hc.get("hdri_path") or hdri_path_raw
                if hdri_strength is None and "hdri_strength" in _hc:
                    hdri_strength = float(_hc.get("hdri_strength"))
            except Exception:
                pass
        if hdri_strength is None:
            hdri_strength = 0.7
        if not hdri_path_raw:
            run_cfg = (cfg.get("run") or {})
            hdri_path_raw = run_cfg.get("hdri_path")
            try:
                if "hdri_strength" in run_cfg and hdri_strength is None:
                    hdri_strength = float(run_cfg.get("hdri_strength", 0.7))
            except Exception:
                pass
        if hdri_path_raw:
            hdri_path = Path(hdri_path_raw).expanduser()
            if not hdri_path.is_absolute():
                hdri_path = (base_dir / hdri_path).resolve()
            if hdri_path.exists():
                apply_hdri_environment(hdri_path, hdri_strength)
            else:
                print(f"[WARN] Configured HDRI not found: {hdri_path}. Falling back to find_missing_files.")
                try_resolve_missing_files(base_dir)
        else:
            try_resolve_missing_files(base_dir)
    except Exception:
        # Never fail the pipeline on HDRI prep
        pass

    for role in ["Disputant1", "MediatorA", "MediatorB", "Disputant2"]:
        if role in cfg.get("characters", {}):
            configure_role(role, cfg, opts["dry_run"])

    if opts["dry_run"]:
        print("[DRY] Completed configuration without saving.")
        return
    if opts["save_as"]:
        print(f"[SAVE] Scene as: {opts['save_as']}")
        bpy.ops.wm.save_as_mainfile(filepath=opts["save_as"], copy=False)
    elif opts["save"]:
        if bpy.data.filepath:
            print(f"[SAVE] Scene in place: {bpy.data.filepath}")
            bpy.ops.wm.save_mainfile(filepath=bpy.data.filepath)
        else:
            print("[WARN] No scene path; use --save-as to specify a destination.")


if __name__ == "__main__":
    main()


