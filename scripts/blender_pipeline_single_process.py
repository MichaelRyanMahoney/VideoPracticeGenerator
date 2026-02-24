#!/usr/bin/env python3
"""
Run Blender scene prep and viseme render in a single Blender process.
This avoids intermediate save/reopen boundaries between Blender subprocesses.
"""

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.resolve()))

import argparse
import json
import os
import shutil
from pathlib import Path

import bpy

import blender_generate_character_files as gen_chars
import blender_configure_roles_for_render as cfg_roles
import blender_export_characters as export_chars
import run_director_visemes as viseme_render


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _copy_scene(src: Path, dst: Path) -> None:
    _ensure_parent(dst)
    shutil.copyfile(src, dst)
    print(f"[single] copied scene: {src} -> {dst}", flush=True)


def _open_scene(scene_path: Path) -> None:
    bpy.ops.wm.open_mainfile(filepath=str(scene_path))
    print(f"[single] opened scene: {scene_path}", flush=True)


def _file_sig(path: Path):
    try:
        st = path.stat()
        return (int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))
    except Exception:
        return None


def _looks_like_blend(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and path.stat().st_size > 0
    except Exception:
        return False


def _save_scene_robust(scene_out: Path) -> None:
    _ensure_parent(scene_out)
    before = _file_sig(scene_out)
    try:
        try:
            bpy.ops.wm.save_as_mainfile(filepath=str(scene_out), copy=True, check_existing=False)
        except TypeError:
            bpy.ops.wm.save_as_mainfile(filepath=str(scene_out), copy=True)
        print(f"[single] saved scene-as: {scene_out}", flush=True)
        return
    except Exception as ex:
        print(f"[single] WARN save_as_mainfile failed: {ex}", flush=True)
    after = _file_sig(scene_out)
    if _looks_like_blend(scene_out) and after and after != before:
        print(f"[single] WARN save_as raised but output changed; continuing: {scene_out}", flush=True)
        return
    # Fallback: save current file and copy bytes
    current = Path(bpy.data.filepath) if bpy.data.filepath else None
    if current and current.exists():
        try:
            bpy.ops.wm.save_mainfile(filepath=str(current))
        except Exception as ex:
            print(f"[single] WARN save_mainfile fallback failed: {ex}", flush=True)
        shutil.copy2(str(current), str(scene_out))
        print(f"[single] copied current scene -> {scene_out}", flush=True)
        return
    raise RuntimeError(f"Failed to save output scene: {scene_out}")


def _assert_role_collections(cfg: dict) -> None:
    chars = cfg.get("characters") or {}
    missing = [role for role in chars.keys() if bpy.data.collections.get(role) is None]
    if missing:
        raise RuntimeError(f"Missing role collections after prep: {missing}")
    print(f"[single] role collections present: {sorted(chars.keys())}", flush=True)


def _snapshot_object_visibility() -> dict[str, tuple[bool, bool]]:
    snap: dict[str, tuple[bool, bool]] = {}
    for obj in bpy.data.objects:
        try:
            hide_render = bool(obj.hide_render)
        except Exception:
            hide_render = False
        try:
            hide_view = bool(obj.hide_get())
        except Exception:
            hide_view = False
        snap[obj.name] = (hide_render, hide_view)
    return snap


def _restore_object_visibility(snap: dict[str, tuple[bool, bool]]) -> None:
    for obj in bpy.data.objects:
        state = snap.get(obj.name)
        if not state:
            continue
        hide_render, hide_view = state
        try:
            obj.hide_render = hide_render
        except Exception:
            pass
        try:
            obj.hide_set(hide_view)
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generator_inputs_json", required=True)
    ap.add_argument("--base_scene_blend", default="")
    ap.add_argument("--default_character_blend", default="")
    ap.add_argument("--work_scene", default="")
    ap.add_argument("--input_scene", default="")
    ap.add_argument("--scene_out", default="")
    ap.add_argument("--director_json", default="")
    ap.add_argument("--out_video", default="")
    ap.add_argument("--max_frame_end", type=int, default=12000)
    ap.add_argument("--frame_start", type=int, default=0)
    ap.add_argument("--frame_end", type=int, default=0)
    ap.add_argument("--engine", default="")
    ap.add_argument("--quality", default="")
    ap.add_argument("--no_audio", action="store_true")
    ap.add_argument("--prepare_only", action="store_true")
    ap.add_argument("--run_generate_characters", action="store_true")
    ap.add_argument("--run_configure_roles", action="store_true")
    ap.add_argument("--run_export_characters", action="store_true")
    ap.add_argument("--export_characters_output_dir", default="")
    ap.add_argument("--export_image_width", type=int, default=1200)
    ap.add_argument("--export_file_prefix", default="Char")
    ap.add_argument("--export_table_object_name", default="")
    ap.add_argument("--export_table_file_prefix", default="Table")
    ap.add_argument("--hdri_path", default="")
    ap.add_argument("--hdri_strength", type=float, default=0.7)
    ap.add_argument("--hdri_from_config", default="")
    ap.add_argument("--trace", action="store_true")
    # In Blender, script arguments are passed after a standalone "--".
    # Falling back to sys.argv[1:] keeps direct local Python execution working.
    argv = sys.argv
    args = argv[argv.index("--") + 1 :] if "--" in argv else argv[1:]
    return ap.parse_args(args)


def main() -> None:
    args = parse_args()
    gen_inputs = Path(args.generator_inputs_json).expanduser().resolve()
    cfg = _load_json(gen_inputs)
    os.environ["VPG_GENERATOR_INPUTS_JSON"] = str(gen_inputs)
    os.environ.setdefault("VPG_USE_SEQUENCER", "0")
    os.environ.setdefault("VPG_USE_COMPOSITING", "0")

    base_scene = Path(args.base_scene_blend).expanduser().resolve() if args.base_scene_blend else None
    default_char = Path(args.default_character_blend).expanduser().resolve() if args.default_character_blend else None
    input_scene = Path(args.input_scene).expanduser().resolve() if args.input_scene else None
    work_scene = Path(args.work_scene).expanduser().resolve() if args.work_scene else None

    if args.run_generate_characters:
        if not base_scene or not base_scene.exists():
            raise RuntimeError(f"Missing base scene for generation: {base_scene}")
        if not default_char or not default_char.exists():
            raise RuntimeError(f"Missing default character blend: {default_char}")
        if not work_scene:
            raise RuntimeError("--work_scene is required when --run_generate_characters is set")
        _copy_scene(base_scene, work_scene)
        gen_opts = {
            "config": str(gen_inputs),
            "source": str(default_char),
            "outdir": str(default_char.parent),
            "dry_run": False,
            "append_scene": str(work_scene),
            "scene_save": False,
            "scene_save_as": None,
        }
        gen_chars.run_with_options(gen_opts)
    else:
        scene_to_open = input_scene or work_scene or base_scene
        if not scene_to_open:
            raise RuntimeError("Need one of --input_scene, --work_scene, or --base_scene_blend")
        _open_scene(scene_to_open)

    if args.run_configure_roles:
        cfg_opts = {
            "config": str(gen_inputs),
            "scene": None,
            "save": False,
            "save_as": None,
            "dry_run": False,
            "trace": bool(args.trace),
            "hdri_path": (args.hdri_path or None),
            "hdri_strength": float(args.hdri_strength),
            "hdri_from_config": (args.hdri_from_config or None),
        }
        cfg_roles.run_with_options(cfg_opts)

    _assert_role_collections(cfg)

    if args.run_export_characters and args.export_characters_output_dir:
        export_dir = Path(args.export_characters_output_dir).expanduser().resolve()
        _ensure_parent(export_dir / "x")
        roles = ["Disputant1", "MediatorA", "MediatorB", "Disputant2"]
        vis_snap = _snapshot_object_visibility()
        try:
            export_chars.run_with_options(
                output_dir=str(export_dir),
                roles=roles,
                file_prefix=args.export_file_prefix,
                image_width=int(args.export_image_width),
                hdri_path=args.hdri_path or "",
                hdri_strength=float(args.hdri_strength),
                generator_inputs_json=str(gen_inputs),
            )
            if args.export_table_object_name:
                export_chars.run_with_options(
                    output_dir=str(export_dir),
                    objects=[args.export_table_object_name],
                    file_prefix=args.export_table_file_prefix,
                    image_width=int(args.export_image_width),
                    hdri_path=args.hdri_path or "",
                    hdri_strength=float(args.hdri_strength),
                    generator_inputs_json=str(gen_inputs),
                )
        finally:
            _restore_object_visibility(vis_snap)
            print("[single] restored object visibility after export", flush=True)

    if not args.prepare_only:
        if not args.director_json or not args.out_video:
            raise RuntimeError("Render mode requires --director_json and --out_video")
        viseme_render.run_with_options(
            str(Path(args.director_json).expanduser().resolve()),
            str(Path(args.out_video).expanduser().resolve()),
            engine=(args.engine or None),
            quality=(args.quality or None),
            frame_start=(int(args.frame_start) if int(args.frame_start) > 0 else None),
            frame_end=(int(args.frame_end) if int(args.frame_end) > 0 else None),
            max_frame_end=int(args.max_frame_end),
            no_audio=bool(args.no_audio),
        )
        out_path = Path(args.out_video).expanduser().resolve()
        frames_dir = out_path.parent / f"{out_path.stem}_frames"
        if not out_path.exists() and not frames_dir.exists():
            raise RuntimeError(f"No render output produced: {out_path} or {frames_dir}")

    if args.scene_out:
        _save_scene_robust(Path(args.scene_out).expanduser().resolve())

    print("[single] done", flush=True)


if __name__ == "__main__":
    main()

