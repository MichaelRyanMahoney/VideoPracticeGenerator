#!/usr/bin/env python3
"""
Render transparent PNGs of specified characters/objects from a .blend file.

Usage (examples):
  blender -b /path/to/scene.blend --python blender_export_characters.py -- \\
    --output-dir /abs/path/to/output \\
    --objects "Disputant1" "MediatorA" "MediatorB" "Disputant2" \\
    --file-prefix "Char" --image-width 1200

Or, role-driven (strictly role-based; no name fallbacks):
  blender -b /path/to/scene.blend --python blender_export_characters.py -- \\
    --output-dir /abs/path/to/output \\
    --roles Disputant1 MediatorA MediatorB Disputant2 \\
    --file-prefix "Char" --image-width 1200

Optional:
- Force a specific camera (defaults to scene camera):
  --camera-name "RenderCam"

Notes
- Requires Blender to run this script (invoked via blender -b ... --python ...).
- Renders each object alone with RGBA on transparent background.
- Keeps the current scene camera. Ensure your objects are visible in camera view.
"""
import argparse
import sys
from pathlib import Path

try:
    import bpy  # type: ignore
except Exception as e:
    print("This script must be run inside Blender (bpy not found).", file=sys.stderr)
    sys.exit(1)


def set_transparent_render(image_width: int | None = None) -> None:
    scene = bpy.context.scene
    render = scene.render
    if image_width and image_width > 0:
        render.resolution_x = int(image_width)
        # preserve aspect ratio
        # render.resolution_y stays as-is, scale_y controlled by percentage to avoid distortion
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGBA"
    render.film_transparent = True  # For Eevee/Cycles transparency
    # Ensure alpha is saved
    if scene.render.engine == "CYCLES":
        scene.cycles.film_transparent = True


def isolate_and_render(obj_name: str, out_path: Path) -> None:
    # Build a lookup of objects present in the active View Layer
    layer_obj_names = {o.name for o in bpy.context.view_layer.objects}
    def safe_hide_set(o, state: bool):
        # Only call hide_set if object exists in active View Layer
        if o.name in layer_obj_names:
            o.hide_set(state)

    # Hide everything
    for obj in bpy.data.objects:
        # Keep cameras/lights visible so framing and lighting stay consistent
        if getattr(obj, "type", None) in {"CAMERA", "LIGHT"}:
            obj.hide_render = False
            safe_hide_set(obj, False)
        else:
            obj.hide_render = True
            safe_hide_set(obj, True)

    # Unhide target object and its parents (if any)
    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        print(f"[blender_export] WARNING: Object not found: {obj_name}")
        return

    # Unhide object hierarchy (parents)
    cur = obj
    chain = []
    while cur is not None:
        chain.append(cur)
        cur = cur.parent
    for o in chain:
        o.hide_render = False
        safe_hide_set(o, False)

    # Also unhide armature/children that belong to this object (common in rigs)
    for child in obj.children_recursive:
        child.hide_render = False
        safe_hide_set(child, False)

    # Render
    bpy.context.scene.render.filepath = str(out_path)
    bpy.ops.render.render(write_still=True)

def isolate_collection_and_render(coll_name: str, out_path: Path) -> None:
    # Build a lookup of objects present in the active View Layer
    layer_obj_names = {o.name for o in bpy.context.view_layer.objects}
    def safe_hide_set(o, state: bool):
        if o.name in layer_obj_names:
            o.hide_set(state)

    # Hide everything
    for obj in bpy.data.objects:
        if getattr(obj, "type", None) in {"CAMERA", "LIGHT"}:
            obj.hide_render = False
            safe_hide_set(obj, False)
        else:
            obj.hide_render = True
            safe_hide_set(obj, True)

    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        print(f"[blender_export] WARNING: Collection not found: {coll_name}")
        return

    def unhide_obj(o):
        # Unhide object and its parents
        cur = o
        while cur is not None:
            cur.hide_render = False
            safe_hide_set(cur, False)
            cur = cur.parent
        # Unhide all children as well
        for child in o.children_recursive:
            child.hide_render = False
            safe_hide_set(child, False)

    # Unhide all objects in collection (recursively)
    def visit_collection(c):
        for o in c.objects:
            unhide_obj(o)
        for ch in c.children:
            visit_collection(ch)
    visit_collection(coll)

    # Render
    bpy.context.scene.render.filepath = str(out_path)
    bpy.ops.render.render(write_still=True)

def find_collection_casefold(names: list[str]):
    lookup = {c.name.casefold(): c for c in bpy.data.collections}
    for n in names:
        if n.casefold() in lookup:
            return lookup[n.casefold()]
    return None

def find_object_casefold(names: list[str]):
    lookup = {o.name.casefold(): o for o in bpy.data.objects}
    for n in names:
        if n.casefold() in lookup:
            return lookup[n.casefold()]
    return None

def _load_roles_from_config(cfg_path: Path) -> list[str]:
    try:
        import json
        data = json.loads(cfg_path.read_text())
        roles = list((data.get("characters") or {}).keys())
        # Preserve common order if present
        order = ["Disputant1", "MediatorA", "MediatorB", "Disputant2"]
        if all(r in roles for r in order):
            return order
        return roles
    except Exception:
        return []

def _normalize_role_name(role_in: str) -> str:
    k = (role_in or "").strip()
    kl = k.lower()
    short_map = {"d1":"Disputant1","d2":"Disputant2","ma":"MediatorA","mb":"MediatorB"}
    return short_map.get(kl, k)

def resolve_role_targets(role_names: list[str], cfg_path: Path | None) -> list[tuple[str, str, str]]:
    """
    Resolve each role strictly by role name: MediatorA, MediatorB, Disputant1, Disputant2.
    Short forms ma/mb/d1/d2 are accepted. No character name fallbacks.
    Returns [(label_for_filename, type, name)]
    """
    allowed = set(_load_roles_from_config(cfg_path) if cfg_path else [])
    results: list[tuple[str, str, str]] = []
    for r in role_names:
        role = _normalize_role_name(r)
        if allowed and role not in allowed:
            print(f"[blender_export] WARNING: Role '{role}' not listed in generator_inputs.json; continuing lookup.")
        coll = find_collection_casefold([role])
        if coll:
            results.append((role, "collection", coll.name))
            continue
        obj = find_object_casefold([role])
        if obj:
            results.append((role, "object", obj.name))
            continue
        print(f"[blender_export] WARNING: Could not resolve role '{role}' to any collection/object (looked for '{role}')")
    return results


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True, help="Directory to write PNGs")
    ap.add_argument("--objects", nargs="+", help="Names of objects to render")
    ap.add_argument("--roles", nargs="+", help="Role names to render: MediatorA MediatorB Disputant1 Disputant2 (short: ma mb d1 d2)")
    ap.add_argument("--file-prefix", default="Char", help="Filename prefix (default Char)")
    ap.add_argument("--image-width", type=int, default=0, help="Output image width (preserves aspect)")
    ap.add_argument("--camera-name", help="Optional: use a specific camera by name (defaults to scene camera)")
    ap.add_argument("--generator_inputs_json", help="Path to manifests/generator_inputs.json for role validation", default=str(Path(__file__).resolve().parents[1] / "manifests" / "generator_inputs.json"))
    args = ap.parse_args(argv)

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    set_transparent_render(args.image_width or None)

    # Optionally set a specific camera
    if args.camera_name:
        cam = bpy.data.objects.get(args.camera_name)
        if cam and getattr(cam, "type", None) == "CAMERA":
            bpy.context.scene.camera = cam
            print(f"[blender_export] Using camera: {cam.name}")
        else:
            print(f"[blender_export] WARNING: Camera '{args.camera_name}' not found or not a CAMERA; using scene camera.")
    else:
        # Auto-select camera if there is exactly one in the file
        cams = [o for o in bpy.data.objects if getattr(o, "type", None) == "CAMERA"]
        if len(cams) == 1:
            bpy.context.scene.camera = cams[0]
            print(f"[blender_export] Auto-selected only camera: {cams[0].name}")

    cfg_path = Path(args.generator_inputs_json).resolve() if args.generator_inputs_json else None
    if args.roles:
        targets = resolve_role_targets(args.roles, cfg_path)
        for idx, (label, ttype, name) in enumerate(targets, start=1):
            safe = "".join(c for c in label if c.isalnum() or c in ("_", "-"))
            out_path = out_dir / f"{args.file_prefix}_{idx:02d}_{safe}.png"
            print(f"[blender_export] Rendering role={label} ({ttype}:{name}) → {out_path}")
            if ttype == "collection":
                isolate_collection_and_render(name, out_path)
            else:
                isolate_and_render(name, out_path)
    elif args.objects:
        for idx, name in enumerate(args.objects, start=1):
            safe = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
            out_path = out_dir / f"{args.file_prefix}_{idx:02d}_{safe}.png"
            print(f"[blender_export] Rendering {name} → {out_path}")
            isolate_and_render(name, out_path)
    else:
        print("[blender_export] ERROR: Provide either --roles (MediatorA MediatorB Disputant1 Disputant2) or --objects <names...>")
        return 2

    print("[blender_export] Done.")
    return 0


if __name__ == "__main__":
    # Blender passes args after '--' to this script
    # Find the separator and pass the rest to argparse
    argv = sys.argv
    if "--" in argv:
        idx = argv.index("--")
        script_args = argv[idx + 1 :]
    else:
        script_args = []
    raise SystemExit(main(script_args))


