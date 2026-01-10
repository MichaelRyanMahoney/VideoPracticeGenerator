import json
import sys
from pathlib import Path
import re

import bpy


# ---------- Arg parsing ----------
def parse_args(default_base: Path) -> dict:
    argv = sys.argv
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    opts = {
        "config": str(default_base / "manifests" / "generator_inputs.json"),
        "source": str(default_base / "assets" / "DefaultCharacter.blend"),
        "outdir": str(default_base / "assets"),
        "dry_run": False,
        "append_scene": None,
        "scene_save": False,
        "scene_save_as": None,
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--config" and i + 1 < len(args):
            opts["config"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        if a == "--source" and i + 1 < len(args):
            opts["source"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        if a == "--outdir" and i + 1 < len(args):
            opts["outdir"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        if a == "--dry-run":
            opts["dry_run"] = True
            i += 1
            continue
        if a == "--append-scene" and i + 1 < len(args):
            opts["append_scene"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        if a == "--scene-save":
            opts["scene_save"] = True
            i += 1
            continue
        if a == "--scene-save-as" and i + 1 < len(args):
            opts["scene_save_as"] = str(Path(args[i + 1]).expanduser().resolve())
            i += 2
            continue
        i += 1
    return opts


# ---------- Config ----------
def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def roles_and_prefixes(cfg: dict) -> list[tuple[str, str]]:
    bm = cfg.get("blender_mapping", {})
    rp = bm.get("role_prefix", {
        "MediatorA": "mA_",
        "MediatorB": "mB_",
        "Disputant1": "d1_",
        "Disputant2": "d2_",
    })
    # preserve order Boy-first used elsewhere: here use a stable order
    wanted = ["MediatorA", "MediatorB", "Disputant1", "Disputant2"]
    return [(r, rp[r]) for r in wanted if r in rp]


# ---------- Renaming helpers (self-contained) ----------
_suffix_re = re.compile(r"^(.*?)(\.\d{3})?$")
_numeric_suffix_re = re.compile(r"\.(\d{3})$")


def split_base_and_suffix(name: str) -> tuple[str, str]:
    m = _suffix_re.match(name)
    if not m:
        return name, ""
    return m.group(1), m.group(2) or ""


def parse_numeric_suffix(name: str) -> int | None:
    m = _numeric_suffix_re.search(name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def strip_known_prefix(name: str, prefixes: list[str]) -> str:
    for p in prefixes:
        if name.startswith(p):
            return name[len(p) :]
    return name


def desired_collection_name(prefix: str, original_name: str, known_prefixes: list[str]) -> str:
    base, _ = split_base_and_suffix(original_name)
    base = strip_known_prefix(base, known_prefixes)
    return f"{prefix}{base}"


def desired_object_name(prefix: str, original_name: str, known_prefixes: list[str]) -> str:
    base, suffix = split_base_and_suffix(original_name)
    base = strip_known_prefix(base, known_prefixes)
    return f"{prefix}{base}{suffix}"


def set_name_safely(id_block, target_name: str, dry_run: bool) -> None:
    if id_block.name == target_name:
        return
    if dry_run:
        print(f"[DRY] {id_block.__class__.__name__}: '{id_block.name}' -> '{target_name}'")
        return
    # temporary unique to avoid collisions
    id_block.name = f"__TMP__{id_block.name}"
    id_block.name = target_name


def collect_descendants(root_col):
    collections = []
    objects = []

    def walk(col):
        for c in col.children:
            collections.append(c)
            walk(c)
        for o in col.objects:
            objects.append(o)

    walk(root_col)
    return collections, objects


def normalize_suffixes_in_collection(col, prefix: str, known_prefixes: list[str], dry_run: bool) -> None:
    groups: dict[str, list[tuple[object, int | None, str]]] = {}
    for obj in list(col.objects):
        base, _ = split_base_and_suffix(obj.name)
        base = strip_known_prefix(base, known_prefixes)
        num = parse_numeric_suffix(obj.name)
        groups.setdefault(base, []).append((obj, num, obj.name))
    for base, items in groups.items():
        items_sorted = sorted(items, key=lambda t: (t[1] if t[1] is not None else -1, t[2]))
        for idx, (obj, _num, _curr) in enumerate(items_sorted):
            suffix = "" if len(items_sorted) == 1 or idx == 0 else f".{idx:03d}"
            target = f"{prefix}{base}{suffix}"
            set_name_safely(obj, target, dry_run)


def _norm_category_name(name: str, known_prefixes: list[str]) -> str:
    base, _ = split_base_and_suffix(name)
    base = strip_known_prefix(base, known_prefixes)
    base = base.strip()
    base_l = base.lower()
    for tail in (" - boy", " - girl"):
        if base_l.endswith(tail):
            base = base[: -len(tail)]
            break
    return base.strip()


def normalize_suffixes_across_gender_categories(root_col, prefix: str, known_prefixes: list[str], dry_run: bool) -> None:
    boy_col = None
    girl_col = None
    for c in root_col.children:
        n = c.name.lower()
        if n.endswith("_boy") or n == "boy":
            boy_col = c
        elif n.endswith("_girl") or n == "girl":
            girl_col = c
    if boy_col is None and girl_col is None:
        return
    by_category: dict[str, list[tuple[str, object]]] = {}
    def add_side(parent_col, gender_tag):
        if parent_col is None:
            return
        for sub in parent_col.children:
            cat = _norm_category_name(sub.name, known_prefixes)
            by_category.setdefault(cat, [])
            by_category[cat].append((gender_tag, sub))
    add_side(boy_col, "boy")
    add_side(girl_col, "girl")
    for _cat, entries in by_category.items():
        entries_sorted = sorted(entries, key=lambda e: 0 if e[0] == "boy" else 1)
        bases: set[str] = set()
        for _gender, col in entries_sorted:
            for obj in list(col.objects):
                b, _ = split_base_and_suffix(obj.name)
                b = strip_known_prefix(b, known_prefixes)
                bases.add(b)
        for base in sorted(bases):
            seq_items: list[bpy.types.Object] = []
            for _gender, col in entries_sorted:
                bucket = []
                for obj in list(col.objects):
                    b, _ = split_base_and_suffix(obj.name)
                    b = strip_known_prefix(b, known_prefixes)
                    if b != base:
                        continue
                    num = parse_numeric_suffix(obj.name)
                    bucket.append((obj, num, obj.name))
                bucket.sort(key=lambda t: (t[1] if t[1] is not None else -1, t[2]))
                seq_items.extend([o for (o, _n, _curr) in bucket])
            for idx, obj in enumerate(seq_items):
                suffix = "" if idx == 0 else f".{idx:03d}"
                target = f"{prefix}{base}{suffix}"
                set_name_safely(obj, target, dry_run)


def apply_prefix_to_default_character(role_name: str, prefix: str, dry_run: bool) -> None:
    known_prefixes = ["mA_", "mB_", "d1_", "d2_"]
    root = bpy.data.collections.get("DefaultCharacter")
    if root is None:
        # fallback: first master collection child
        for c in bpy.context.scene.collection.children:
            if c.name == "DefaultCharacter":
                root = c
                break
    if root is None:
        print("[ERR] Root collection 'DefaultCharacter' not found.")
        return
    # Rename root collection to role_name
    set_name_safely(root, role_name, dry_run)
    # Collect descendants
    collections, objects = collect_descendants(root)
    # Apply collection prefixes (drop numeric suffixes)
    for c in collections:
        target = desired_collection_name(prefix, c.name, known_prefixes)
        set_name_safely(c, target, dry_run)
    # Apply object prefixes (keep current suffixes for first pass)
    for o in objects:
        target = desired_object_name(prefix, o.name, known_prefixes)
        set_name_safely(o, target, dry_run)
    # Normalize per collection
    for col in [root] + collections:
        normalize_suffixes_in_collection(col, prefix, known_prefixes, dry_run)
    # And across Boy/Girl for shared bases
    normalize_suffixes_across_gender_categories(root, prefix, known_prefixes, dry_run)

    # Clear animation on this character only (preserve shapekeys and pose bones)
    clear_animation_for_character(root, dry_run)

    # Prefix materials used by this character so they are role-unique
    prefix_materials_under_role(root, prefix, known_prefixes, dry_run)


def prefix_materials_under_role(root_col, prefix: str, known_prefixes: list[str], dry_run: bool) -> None:
    # Map to reuse copies of the same source material within this character
    source_to_copy = {}

    def normalize_material_name(mat_name: str) -> str:
        base, _ = split_base_and_suffix(mat_name)
        base = strip_known_prefix(base, known_prefixes)
        return f"{prefix}{base}"

    collections, objects = collect_descendants(root_col)
    for obj in [o for o in objects if hasattr(o, "material_slots")]:
        for slot in obj.material_slots:
            mat = slot.material
            if not mat:
                continue
            # Skip if already prefixed for this role
            if mat.name.startswith(prefix):
                continue
            # Reuse copy if we've already duplicated this source material
            if mat in source_to_copy:
                new_mat = source_to_copy[mat]
            else:
                new_name = normalize_material_name(mat.name)
                if dry_run:
                    print(f"[DRY] Material copy '{mat.name}' -> '{new_name}'")
                    # Simulate by not changing slot
                    continue
                new_mat = mat.copy()
                new_mat.name = new_name
                source_to_copy[mat] = new_mat
            if not dry_run:
                slot.material = new_mat


# ---------- Animation clearing (character-scoped) ----------
def clear_action_keyframes(action, dry_run: bool, keep_prefixes: list[str] | None = None) -> int:
    if action is None:
        return 0
    count = 0
    for fcu in list(action.fcurves):
        dp = fcu.data_path or ""
        if keep_prefixes and any(dp.startswith(p) for p in keep_prefixes):
            continue
        kcount = len(getattr(fcu, "keyframe_points", []))
        count += kcount
        if dry_run:
            print(f"[DRY] Remove FCurve '{dp}' [{kcount} keys]")
        else:
            action.fcurves.remove(fcu)
    return count


def clear_object_keyframes_scoped(obj: bpy.types.Object, dry_run: bool) -> int:
    removed = 0
    ad = obj.animation_data
    if ad:
        keep_prefixes = ['pose.bones['] if obj.type == 'ARMATURE' else None
        removed += clear_action_keyframes(ad.action, dry_run, keep_prefixes=keep_prefixes)
        # Keep NLA for armatures to preserve pose strips if present
        if obj.type != 'ARMATURE' and ad.nla_tracks:
            if dry_run:
                print(f"[DRY] Remove {len(ad.nla_tracks)} NLA tracks from '{obj.name}'")
            else:
                for tr in list(ad.nla_tracks):
                    ad.nla_tracks.remove(tr)
    return removed


def clear_collection_keyframes_scoped(col: bpy.types.Collection, dry_run: bool) -> int:
    ad = getattr(col, "animation_data", None)
    if not ad:
        return 0
    return clear_action_keyframes(ad.action, dry_run)


def clear_animation_for_character(root_col: bpy.types.Collection, dry_run: bool) -> None:
    collections, objects = collect_descendants(root_col)
    obj_total = 0
    for o in objects:
        obj_total += clear_object_keyframes_scoped(o, dry_run)
    col_total = 0
    for c in [root_col] + collections:
        col_total += clear_collection_keyframes_scoped(c, dry_run)
    print(f"[OK] Cleared keyframes for character '{root_col.name}' "
          f"(objects:{obj_total}, collections:{col_total}).")


def main():
    base_dir = Path(__file__).resolve().parents[1]
    opts = parse_args(base_dir)
    cfg = load_config(opts["config"])
    pairs = roles_and_prefixes(cfg)
    source = opts["source"]
    outdir = Path(opts["outdir"])
    outdir.mkdir(parents=True, exist_ok=True)
    dry = opts["dry_run"]

    for role, prefix in pairs:
        # Load fresh source for each role so changes don't accumulate
        bpy.ops.wm.open_mainfile(filepath=source)
        apply_prefix_to_default_character(role, prefix, dry)
        dest = outdir / f"{role}.blend"
        if dry:
            print(f"[DRY] Would save copy: {dest}")
        else:
            print(f"[SAVE] {dest}")
            bpy.ops.wm.save_as_mainfile(filepath=str(dest), copy=False)

    print("[OK] Generated role files.")

    # Optionally append into a scene and position
    if opts["append_scene"]:
        scene_path = opts["append_scene"]
        print(f"[SCENE] Opening scene: {scene_path}")
        bpy.ops.wm.open_mainfile(filepath=scene_path)

        role_to_pos = {
            "Disputant1": (-3.5, -1.2, 4.0),
            "MediatorA": (0.6, 1.1, 5.4),
            "MediatorB": (5.1, 1.1, 5.4),
            "Disputant2": (9.4, -1.2, 4.0),
        }
        order = ["Disputant1", "MediatorA", "MediatorB", "Disputant2"]

        def append_collection_from(blend_file: Path, coll_name: str):
            dir_path = str(blend_file) + "/Collection"
            bpy.ops.wm.append(
                directory=dir_path,
                filename=coll_name,
                link=False,
                autoselect=False,
                active_collection=False,
                set_fake=False,
            )

        def link_under_scene_root(col):
            scene = bpy.context.scene
            if col.name not in [c.name for c in scene.collection.children]:
                scene.collection.children.link(col)

        def iter_objects_in_collection(col):
            # Recursively yields all objects in collection and sub-collections
            for o in col.objects:
                yield o
            for c in col.children:
                yield from iter_objects_in_collection(c)

        def place_role(col_name: str, loc):
            col = bpy.data.collections.get(col_name)
            if not col:
                print(f"[WARN] Appended collection '{col_name}' not found.")
                return
            # Move out of any auto 'Appended Data' parents and link under scene root
            parents = []
            for p in bpy.data.collections:
                try:
                    if any(c == col for c in p.children):
                        parents.append(p)
                except Exception:
                    pass
            for p in list(parents):
                if p.name.startswith("Appended Data"):
                    try:
                        p.children.unlink(col)
                    except Exception:
                        pass
            link_under_scene_root(col)
            # Clean up empty Appended Data containers
            for p in list(parents):
                if p.name.startswith("Appended Data"):
                    if len(p.objects) == 0 and len(p.children) == 0:
                        try:
                            bpy.context.scene.collection.children.unlink(p)
                        except Exception:
                            pass
                        try:
                            bpy.data.collections.remove(p)
                        except Exception:
                            pass
            # Move all armatures to the desired location
            moved = 0
            for obj in iter_objects_in_collection(col):
                if obj.type == 'ARMATURE':
                    obj.location = loc
                    moved += 1
            print(f"[POS] '{col_name}' placed at {loc} (moved {moved} armature(s)).")

        # Append and position in order
        for role in order:
            blend_path = outdir / f"{role}.blend"
            if not blend_path.exists():
                print(f"[WARN] Missing character file: {blend_path}")
                continue
            append_collection_from(blend_path, role)
            place_role(role, role_to_pos[role])

        # Save scene if requested
        if dry:
            print("[DRY] Would save scene with appended characters.")
        else:
            if opts["scene_save_as"]:
                print(f"[SAVE] Scene as: {opts['scene_save_as']}")
                bpy.ops.wm.save_as_mainfile(filepath=opts["scene_save_as"], copy=False)
            elif opts["scene_save"]:
                if bpy.data.filepath:
                    print(f"[SAVE] Scene in place: {bpy.data.filepath}")
                    bpy.ops.wm.save_mainfile(filepath=bpy.data.filepath)
                else:
                    print("[WARN] No scene path; use --scene-save-as.")


if __name__ == "__main__":
    main()


