# Ensure we can import the mapping beside this script
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).parent.resolve()))

import bpy, json, argparse, random
from pathlib import Path
import os
from ovr_viseme_map import OVR_VISEME_KEYS

# -----------------------
# Feature toggles & timing
# -----------------------
ENABLE_BLINKS = True
LEAD_FRAMES = 2
ATTACK_FRAMES = 4
RELEASE_FRAMES = 6
MIN_HOLD_FRAMES = 3
MAX_OVERLAP_FRAMES = 6
TIME_OFFSET_SEC = -0.09

# Optional CLI overrides
CLI_LEAD_FRAMES = None
CLI_TIME_OFFSET_SEC = None
CLI_SMOOTH_FACTOR = None
CLI_ENGINE = None
CLI_PREPARE_VIEWPORT_BLEND = None
CLI_VIEWPORT_RENDER = False
CLI_NO_RENDER = False
CLI_TRANSPARENT = False
CLI_OPAQUE = False
CLI_FRAMES = False
CLI_FRAME_START = None
CLI_FRAME_END = None
CLI_MAX_FRAME_END = None
CLI_NO_CLEAN_FRAMES = False
CLI_NO_AUDIO = False

# Only animate these viseme keys (exact names on your shapekeys)
ALLOWED_KEYS = set(OVR_VISEME_KEYS)

# Optional blink keys (if present on the eyes/body mesh)
BLINK_KEYS = ["eyeBlinkLeft", "eyeBlinkRight"]

# Allow blink keys as well so idle blinks can be keyed
ALLOWED_KEYS |= set(BLINK_KEYS)

# -----------------------
# Utilities
# -----------------------
def clear_shape_key_animation(mesh_obj):
    sk = mesh_obj.data.shape_keys
    if sk and sk.animation_data:
        sk.animation_data_clear()

def zero_all_shapes(mesh_obj, frame):
    sk = mesh_obj.data.shape_keys
    if not sk:
        return
    for kb in sk.key_blocks:
        kb.value = 0.0
        kb.keyframe_insert("value", frame=frame)

def _get_char_parts_for_name(char_name: str):
    parts = {}
    for part in ("body", "teeth", "eyes", "hair"):
        obj = _find_object_loose(f"{char_name}_{part}")
        if obj:
            parts[part] = obj
    return parts

def _get_char_parts_from_mesh_name(mesh_name: str, fallback_char_name: str = None):
    parts = {}
    obj = _find_object_loose(mesh_name) if mesh_name else None
    if obj:
        parts["body"] = obj
    if fallback_char_name:
        name_parts = _get_char_parts_for_name(fallback_char_name)
        parts.update(name_parts)
    return parts

def _get_char_parts_for_role(role: str, role_prefix: str, gender: str):
    """
    Resolve character mesh parts based on role mapping. Object names are expected to follow:
      <rolePrefix>geo_body(.###)
      <rolePrefix>geo_teeth(.###)
      <rolePrefix>geo_{boy|girl}_eyes(.###)
      <rolePrefix>geo_{boy|girl}_nose(.###)   (optional, not targeted directly for visemes)
    Gender 'M' -> boy, others -> girl.
    """
    def _find_best(prefix_name: str):
        """
        Prefer an object whose name matches prefix or prefix.### and that
        actually carries viseme shape keys. Fall back to any match with shape keys,
        then any match.
        """
        if not prefix_name:
            return None
        lname = prefix_name.lower()
        exact = bpy.data.objects.get(prefix_name) or bpy.data.objects.get(prefix_name.lower()) or bpy.data.objects.get(prefix_name.upper()) or bpy.data.objects.get(prefix_name.title())
        # Collect candidates that match prefix or prefix.###
        candidates = []
        for obj in bpy.data.objects:
            on = getattr(obj, "name", "")
            oln = on.lower()
            if oln == lname or oln.startswith(lname + "."):
                candidates.append(obj)
        # If an exact match exists and has shapekeys, pick it
        def has_keys(o):
            try:
                sk = getattr(getattr(o, "data", None), "shape_keys", None)
                return bool(sk and getattr(sk, "key_blocks", None))
            except Exception:
                return False
        def has_viseme_keys(o):
            try:
                sk = getattr(getattr(o, "data", None), "shape_keys", None)
                kbs = getattr(sk, "key_blocks", None)
                if not kbs:
                    return False
                for k in ALLOWED_KEYS:
                    if k in kbs:
                        return True
                return False
            except Exception:
                return False
        # Prefer objects that actually have the required viseme keys
        if exact and has_viseme_keys(exact):
            return exact
        for o in candidates:
            if has_viseme_keys(o):
                return o
        # Otherwise prefer any with shapekeys
        if exact and has_keys(exact):
            return exact
        for o in candidates:
            if has_keys(o):
                return o
        # If no shapekey carriers, return exact or first candidate
        if exact:
            return exact
        return candidates[0] if candidates else None

    parts = {}
    prefix = role_prefix or ""
    # Core body/teeth parts (prefer .001 suffixed meshes)
    body = _find_best(f"{prefix}geo_body.001") or _find_best(f"{prefix}geo_body")
    if body:
        parts["body"] = body
    teeth = _find_best(f"{prefix}geo_teeth.001") or _find_best(f"{prefix}geo_teeth")
    if teeth:
        parts["teeth"] = teeth
    # Eyes depend on gendered geo
    gender_key = "boy" if (str(gender or "").upper().startswith("M")) else "girl"
    eyes = _find_best(f"{prefix}geo_{gender_key}_eyes")
    if not eyes:
        # Fallback to generic eyes if present
        eyes = _find_best(f"{prefix}geo_eyes")
    if eyes:
        parts["eyes"] = eyes
    return parts

def _find_object_loose(name: str):
    if not name:
        return None
    for cand in (name, name.upper(), name.lower(), name.title()):
        obj = bpy.data.objects.get(cand)
        if obj:
            return obj
    lname = name.lower()
    for obj in bpy.data.objects:
        on = obj.name
        oln = on.lower()
        if oln == lname or oln.startswith(lname + "."):
            return obj
    return None

def _for_each_part(parts_dict, fn):
    for _name, _obj in parts_dict.items():
        try:
            fn(_obj)
        except Exception:
            pass

EYE_KEYS = {"eyeBlinkLeft", "eyeBlinkRight"}

def _target_parts_for_key(parts_dict, key):
    if key in EYE_KEYS and parts_dict.get("eyes"):
        return [parts_dict["eyes"]]
    targets = []
    for name in ("body", "teeth"):
        if parts_dict.get(name):
            targets.append(parts_dict[name])
    if not targets and parts_dict:
        targets = [next(iter(parts_dict.values()))]
    return targets

def key_shape(mesh_obj, key, value, frame):
    if key not in ALLOWED_KEYS:
        return False
    kb = mesh_obj.data.shape_keys
    if not kb or key not in kb.key_blocks:
        return False
    k = kb.key_blocks[key]
    k.value = value
    k.keyframe_insert("value", frame=frame)
    return True

def set_key_bezier(mesh_obj, key, value, frame):
    if not key_shape(mesh_obj, key, value, frame):
        return
    sk = mesh_obj.data.shape_keys
    act = sk.animation_data and sk.animation_data.action
    if not act:
        return
    fcurve = act.fcurves.find(f'key_blocks["{key}"].value')
    if fcurve and fcurve.keyframe_points:
        kp = fcurve.keyframe_points[-1]
        kp.interpolation = 'BEZIER'
        try:
            kp.handle_left_type = 'AUTO_CLAMPED'
            kp.handle_right_type = 'AUTO_CLAMPED'
        except Exception:
            pass

def set_key_bezier_multi(parts_dict, key, value, frame):
    targets = _target_parts_for_key(parts_dict, key)
    for obj in targets:
        set_key_bezier(obj, key, value, frame)

def clear_vse():
    scn = bpy.context.scene
    se = scn.sequence_editor
    if se:
        for s in list(se.sequences):
            se.sequences.remove(s)

def tc_to_frame(tc, fps):
    h, m, s = tc.split(":")
    sec = float(h) * 3600 + float(m) * 60 + float(s)
    return int(round(sec * fps))


def tc_to_seconds(tc: str) -> float:
    try:
        h, m, s = (tc or "00:00:00.000").split(":")
        return float(h) * 3600 + float(m) * 60 + float(s)
    except Exception:
        return 0.0


def estimate_timeline_end_seconds(beats: list, fallback_line_sec: float = 1.0) -> float:
    """
    Estimate end time when audio is not available/desired (e.g., distributed render workers).
    We use:
      - pause beats: tc_in + duration
      - speech beats: max(tc_in + max(viseme.t), tc_in + fallback_line_sec)
    """
    end_s = 0.0
    for b in beats or []:
        tc_in = b.get("tc_in") or "00:00:00.000"
        t0 = tc_to_seconds(tc_in)
        if (b.get("type") or "").lower() == "pause" or not b.get("audio"):
            try:
                dur = float(b.get("duration", 1.0))
            except Exception:
                dur = 1.0
            end_s = max(end_s, t0 + max(0.0, dur))
            continue
        # spoken beat
        vmax = 0.0
        try:
            for ev in (b.get("visemes") or []):
                try:
                    vmax = max(vmax, float(ev.get("t", 0.0)))
                except Exception:
                    pass
        except Exception:
            vmax = 0.0
        end_s = max(end_s, t0 + max(vmax - t0, fallback_line_sec))
    return float(end_s)

def ensure_seq():
    scn = bpy.context.scene
    return scn.sequence_editor or scn.sequence_editor_create()

def add_audio(filepath, frame_start, channel=1):
    seq = ensure_seq()
    abs_path = str(Path(filepath).resolve())
    bpy.data.sounds.load(abs_path, check_existing=True)
    strip = seq.sequences.new_sound(
        name=Path(abs_path).stem,
        filepath=abs_path,
        channel=channel,
        frame_start=frame_start
    )
    strip.mute = False
    strip.volume = 1.0
    return strip

def event_window(f_center, next_f=None):
    hold = MIN_HOLD_FRAMES
    if next_f is not None:
        gap = max(0, next_f - f_center)
        hold = min(max(MIN_HOLD_FRAMES, gap // 2), MIN_HOLD_FRAMES + MAX_OVERLAP_FRAMES)
    f_attack = f_center - ATTACK_FRAMES
    f_release = f_center + hold + RELEASE_FRAMES
    return f_attack, f_center, f_release

def apply_visemes_multi(parts_dict, visemes, fps):
    n = len(visemes)
    for i, v in enumerate(visemes):
        key = v["p"]  # exact viseme shapekey name
        t = float(v["t"]) + TIME_OFFSET_SEC
        f_center = int(round(t * fps)) - LEAD_FRAMES
        next_f = int(round(float(visemes[i+1]["t"]) * fps)) - LEAD_FRAMES if i+1<n else None

        if key not in ALLOWED_KEYS:
            continue

        f_attack, f_peak, f_release = event_window(f_center, next_f)
        # Zero other visemes at the edges to keep shapes clean
        for other in ALLOWED_KEYS:
            if other == key:
                continue
            set_key_bezier_multi(parts_dict, other, 0.0, f_attack)
            set_key_bezier_multi(parts_dict, other, 0.0, f_release)
        # Target viseme envelope
        set_key_bezier_multi(parts_dict, key, 0.0, f_attack)
        set_key_bezier_multi(parts_dict, key, 1.0, f_peak)
        set_key_bezier_multi(parts_dict, key, 0.0, f_release)

def add_idle_blinks(parts_dict, fps, start_f, end_f, every_seconds=(3,6)):
    if not ENABLE_BLINKS:
        return
    # Prefer eyes if present; else first available part
    targets = []
    if parts_dict.get("eyes"):
        targets = [parts_dict["eyes"]]
    elif parts_dict:
        targets = [next(iter(parts_dict.values()))]
    if not targets:
        return
    left, right = BLINK_KEYS
    cur = start_f + int(every_seconds[0] * fps)
    while cur < end_f - int(every_seconds[0] * fps):
        gap = random.uniform(*every_seconds)
        f = int(cur + gap * fps)
        for obj in targets:
            for key in (left, right):
                sk = obj.data.shape_keys
                if sk and sk.key_blocks.get(key):
                    set_key_bezier(obj, key, 0.0, f - 2)
                    set_key_bezier(obj, key, 1.0, f)
                    set_key_bezier(obj, key, 0.0, f + 2)
        cur = f

# -----------------------
# Materials / visibility helpers
# -----------------------
def _ensure_principled_alpha_animatable(mat):
    try:
        mat.use_nodes = True
    except Exception:
        return None
    nt = getattr(mat, "node_tree", None)
    if not nt:
        return None
    # find Principled BSDF node
    node = None
    for n in nt.nodes:
        if getattr(n, "type", "") == "BSDF_PRINCIPLED":
            node = n
            break
    if not node:
        return None
    # Ensure Eevee transparency enabled
    try:
        mat.blend_method = 'BLEND'
    except Exception:
        pass
    return node

def fade_object_materials(obj, frame_start, frame_end, from_alpha=0.0, to_alpha=1.0):
    # Iterate all material slots; if Principled node present, animate its Alpha
    for slot in getattr(obj, "material_slots", []) or []:
        mat = slot.material
        if not mat:
            continue
        node = _ensure_principled_alpha_animatable(mat)
        if not node:
            continue
        try:
            alpha_input = node.inputs.get("Alpha")
            if alpha_input is None:
                continue
            # Keyframe alpha at start/end
            alpha_input.default_value = float(from_alpha)
            mat.node_tree.nodes.update()
            try:
                alpha_input.keyframe_insert("default_value", frame=frame_start)
            except Exception:
                pass
            alpha_input.default_value = float(to_alpha)
            mat.node_tree.nodes.update()
            try:
                alpha_input.keyframe_insert("default_value", frame=frame_end)
            except Exception:
                pass
        except Exception:
            continue

# -----------------------
# Main
# -----------------------
def main(director_path, outpath):
    data = json.loads(Path(director_path).read_text())
    scene = bpy.context.scene

    # Prefer the job-provided generator inputs path if set (worker_render sets this),
    # otherwise fall back to the repo default.
    project_root = Path(__file__).resolve().parent.parent
    _gen_inputs_env = (os.environ.get("VPG_GENERATOR_INPUTS_JSON") or os.environ.get("VPG_GENERATOR_INPUTS_JSON_PATH") or "").strip()
    gen_inputs_path = Path(_gen_inputs_env).expanduser() if _gen_inputs_env else (project_root / "manifests" / "generator_inputs.json")
    gen_inputs = {}
    if gen_inputs_path.exists():
        try:
            gen_inputs = json.loads(gen_inputs_path.read_text())
        except Exception:
            gen_inputs = {}
    print(f"[vpg] generator_inputs_path={gen_inputs_path} exists={gen_inputs_path.exists()}", flush=True)
    def _log_cycles_devices(tag: str) -> None:
        try:
            cprefs = bpy.context.preferences.addons["cycles"].preferences
            devs = []
            for d in getattr(cprefs, "devices", []) or []:
                devs.append(
                    {
                        "name": getattr(d, "name", ""),
                        "type": getattr(d, "type", ""),
                        "use": bool(getattr(d, "use", False)),
                    }
                )
            compute_type = getattr(cprefs, "compute_device_type", None)
            scene_device = getattr(getattr(scene, "cycles", None), "device", None)
            print(f"[cycles] {tag}: compute_device_type={compute_type} scene.cycles.device={scene_device} devices={devs}")
        except Exception as ex:
            print(f"[cycles] {tag}: (unable to log devices) {ex}")

    # Timing tunables (JSON-configurable and CLI-overridable)
    global LEAD_FRAMES, ATTACK_FRAMES, RELEASE_FRAMES, MIN_HOLD_FRAMES, MAX_OVERLAP_FRAMES, TIME_OFFSET_SEC
    timing = data.get("timing", {})
    LEAD_FRAMES = int(timing.get("lead_frames", LEAD_FRAMES))
    ATTACK_FRAMES = int(timing.get("attack_frames", ATTACK_FRAMES))
    RELEASE_FRAMES = int(timing.get("release_frames", RELEASE_FRAMES))
    MIN_HOLD_FRAMES = int(timing.get("min_hold_frames", MIN_HOLD_FRAMES))
    MAX_OVERLAP_FRAMES = int(timing.get("max_overlap_frames", MAX_OVERLAP_FRAMES))
    TIME_OFFSET_SEC = float(timing.get("time_offset_sec", TIME_OFFSET_SEC))
    smooth_factor = float(timing.get("smooth_factor", 1.0))

    if CLI_LEAD_FRAMES is not None:
        LEAD_FRAMES = int(CLI_LEAD_FRAMES)
    if CLI_TIME_OFFSET_SEC is not None:
        TIME_OFFSET_SEC = float(CLI_TIME_OFFSET_SEC)
    if CLI_SMOOTH_FACTOR is not None:
        smooth_factor = float(CLI_SMOOTH_FACTOR)
    if smooth_factor and smooth_factor != 1.0:
        ATTACK_FRAMES = max(1, int(round(ATTACK_FRAMES * smooth_factor)))
        RELEASE_FRAMES = max(2, int(round(RELEASE_FRAMES * smooth_factor)))
        MAX_OVERLAP_FRAMES = max(0, int(round(MAX_OVERLAP_FRAMES * smooth_factor)))

    # Render settings
    #
    # IMPORTANT: If `use_sequencer` is True and the VSE contains no video strips (common in
    # our pipeline, especially when running with --no_audio), Blender will render black frames.
    # We therefore disable sequencer rendering by default and only enable it when explicitly requested.
    use_sequencer = (os.environ.get("VPG_USE_SEQUENCER") or "0").strip() == "1"
    try:
        scene.render.use_sequencer = bool(use_sequencer)
    except Exception:
        pass
    # Transparent background toggle (JSON: render.transparent, CLI: --transparent/--opaque)
    # Default behavior: render PNG frames with alpha (transparent=True) unless explicitly overridden.
    _render_cfg = (data.get("render") or {})
    if CLI_OPAQUE:
        transparent = False
    elif CLI_TRANSPARENT:
        transparent = True
    elif "transparent" in _render_cfg:
        try:
            transparent = bool(_render_cfg.get("transparent"))
        except Exception:
            transparent = True
    else:
        transparent = True
    try:
        scene.render.film_transparent = bool(transparent)
    except Exception:
        pass
    # Decide output mode: video vs image sequence
    # - `--frames` forces PNG output regardless of transparency (useful for debugging).
    output_frames = bool(CLI_FRAMES or transparent)
    if output_frames:
        # Use PNG frames; RGBA if transparent, else RGB
        scene.render.image_settings.file_format = "PNG"
        try:
            scene.render.image_settings.color_mode = "RGBA" if transparent else "RGB"
        except Exception:
            pass
        try:
            scene.render.image_settings.color_depth = "8"
        except Exception:
            pass
    else:
        # Default: MP4 video output
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        scene.render.ffmpeg.constant_rate_factor = "HIGH"
        scene.render.ffmpeg.audio_codec = "AAC"
        scene.render.ffmpeg.audio_bitrate = 192000
        scene.render.ffmpeg.audio_channels = "STEREO"

    # Select render engine with precedence: CLI > generator_inputs.json > director JSON > default
    # Normalize generator_inputs run.render_engine if provided
    engine_from_gen = None
    try:
        _run_cfg = (gen_inputs.get("run") or {})
        _re = str(_run_cfg.get("render_engine") or "").strip().lower()
        if _re in ("blender_eevee", "eevee"):
            engine_from_gen = "eevee"
        elif _re in ("blender_workbench", "workbench"):
            engine_from_gen = "workbench"
        elif _re in ("blender_cycles", "cycles"):
            engine_from_gen = "cycles"
    except Exception:
        engine_from_gen = None
    # Director JSON engine (if any)
    engine_from_director = (data.get("render", {}).get("engine") or "").strip().lower()
    if engine_from_director in ("blender_eevee",):
        engine_from_director = "eevee"
    elif engine_from_director in ("blender_workbench",):
        engine_from_director = "workbench"
    elif engine_from_director in ("blender_cycles",):
        engine_from_director = "cycles"
    # Compute final engine
    engine_opt = (CLI_ENGINE or engine_from_gen or engine_from_director or "eevee")
    engine_opt = engine_opt.lower()
    if engine_opt in ("workbench", "blender_workbench"):
        scene.render.engine = "BLENDER_WORKBENCH"
    elif engine_opt in ("cycles", "blender_cycles"):
        scene.render.engine = "CYCLES"
        # Configure Cycles GPU (prefer OPTIX, fallback CUDA)
        try:
            # Pull run settings (samples) if available
            run_cfg = gen_inputs.get("run") or {}
            desired_samples = int(run_cfg.get("samples", 64))
            denoise_enabled = bool(run_cfg.get("denoise", True))
            denoiser_pref = str(run_cfg.get("denoiser", "OPTIX")).strip().upper()
        except Exception:
            desired_samples = 64
            denoise_enabled = True
            denoiser_pref = "OPTIX"
        try:
            prefs = bpy.context.preferences
            cprefs = prefs.addons["cycles"].preferences
            # Try OPTIX first
            for backend in ("OPTIX", "CUDA"):
                try:
                    cprefs.compute_device_type = backend
                    # Refresh devices
                    try:
                        cprefs.get_devices()
                    except Exception:
                        pass
                    # Enable only devices matching the chosen backend.
                    any_backend_device = False
                    for dev in getattr(cprefs, "devices", []) or []:
                        use = bool(getattr(dev, "type", None) == backend)
                        try:
                            dev.use = use
                        except Exception:
                            pass
                        if use:
                            any_backend_device = True
                    # If Blender didn't expose any devices for this backend, try the next backend.
                    if not any_backend_device:
                        _log_cycles_devices(f"backend={backend} has no matching devices; trying next backend")
                        continue
                    try:
                        scene.cycles.device = "GPU"
                    except Exception:
                        pass
                    _log_cycles_devices(f"selected backend={backend}")
                    break
                except Exception:
                    continue
            # Samples
            try:
                scene.cycles.samples = int(desired_samples)
            except Exception:
                pass
            # Denoising (huge speedup at low samples). Prefer OPTIX on NVIDIA if available.
            try:
                # Blender typically stores denoising on the View Layer in Cycles.
                vl = bpy.context.view_layer
                if hasattr(vl, "cycles"):
                    try:
                        vl.cycles.use_denoising = bool(denoise_enabled)
                    except Exception:
                        pass
                    if denoise_enabled:
                        # Only set denoiser if the property exists (varies across versions)
                        for target in (vl.cycles, scene.cycles):
                            try:
                                if hasattr(target, "denoiser"):
                                    # OPTIX (fast on NVIDIA) or OPENIMAGEDENOISE
                                    if denoiser_pref in ("OPTIX", "OPENIMAGEDENOISE"):
                                        target.denoiser = denoiser_pref
                            except Exception:
                                pass
            except Exception:
                pass
            # Reuse Cycles caches across frames; helps when rendering many frames.
            try:
                scene.render.use_persistent_data = True
            except Exception:
                pass
            # Adaptive can help
            try:
                scene.cycles.use_adaptive_sampling = True
            except Exception:
                pass
            # Warn if we failed to enable any GPU devices (common when container/host lacks NVIDIA runtime)
            try:
                any_gpu = False
                for d in getattr(cprefs, "devices", []) or []:
                    if getattr(d, "type", None) in ("OPTIX", "CUDA") and bool(getattr(d, "use", False)):
                        any_gpu = True
                        break
                if not any_gpu:
                    _log_cycles_devices("WARNING no CUDA/OPTIX devices enabled; Cycles will likely render on CPU")
            except Exception:
                pass
        except Exception as ex:
            print(f"[cycles] Warning: failed to configure GPU devices: {ex}")
    else:
        scene.render.engine = "BLENDER_EEVEE"
    # We will print final engine/settings after we apply any generator_inputs overrides + quality preset.

    fps = int(data.get("fps", 24))
    scene.render.fps = fps
    scene.render.resolution_x = data.get("render", {}).get("resolution", [1920,1080])[0]
    scene.render.resolution_y = data.get("render", {}).get("resolution", [1920,1080])[1]

    # Quality preset passthrough (fast/full) like the original script
    def apply_quality_preset(scene, quality: str):
        q = (quality or "full").lower()
        def set_attr(obj, name, value):
            try:
                if hasattr(obj, name):
                    setattr(obj, name, value)
            except Exception:
                pass
        if q == "fast":
            set_attr(scene.render, "resolution_percentage", 60)
            ee = scene.eevee
            set_attr(ee, "use_gtao", False)
            set_attr(ee, "use_ssr", False)
            set_attr(ee, "use_bloom", False)
            set_attr(ee, "use_volumetrics", False)
            set_attr(ee, "use_volumetric_lights", False)
            set_attr(ee, "use_soft_shadows", False)
            set_attr(ee, "use_motion_blur", False)
            set_attr(ee, "taa_samples", 4)
            set_attr(ee, "taa_render_samples", 24)
            set_attr(ee, "shadow_cube_size", "256")
            set_attr(ee, "shadow_cascade_size", "512")
            set_attr(scene.render, "use_simplify", True)
            set_attr(scene.render, "simplify_subdivision", 0)
            set_attr(scene.render, "simplify_child_particles", 0.0)
            set_attr(scene.render, "simplify_volumes", 0.0)
            set_attr(scene.render.ffmpeg, "constant_rate_factor", "MEDIUM")
            set_attr(scene.render, "use_persistent_data", True)
        else:
            set_attr(scene.render, "resolution_percentage", 100)
            ee = scene.eevee
            set_attr(ee, "use_gtao", True)
            set_attr(ee, "use_ssr", True)
            set_attr(ee, "use_bloom", True)
            set_attr(ee, "use_volumetrics", True)
            set_attr(ee, "use_volumetric_lights", True)
            set_attr(ee, "use_soft_shadows", True)
            set_attr(ee, "taa_samples", 32)
            set_attr(ee, "taa_render_samples", 24)
            set_attr(ee, "shadow_cube_size", "1024")
            set_attr(ee, "shadow_cascade_size", "2048")
            set_attr(scene.render, "use_simplify", False)
            set_attr(scene.render.ffmpeg, "constant_rate_factor", "HIGH")

    quality = data.get("render", {}).get("quality", "full")
    if scene.render.engine == "BLENDER_EEVEE":
        apply_quality_preset(scene, quality)
    else:
        # Configure Workbench to mimic Material Preview feel
        try:
            sh = scene.display.shading
            sh.light = 'STUDIO'
            sh.color_type = 'MATERIAL'
            setattr(sh, "show_cavity", getattr(sh, "show_cavity", False))
            setattr(sh, "show_object_outline", getattr(sh, "show_object_outline", False))
        except Exception:
            pass

    beats = data.get("beats", [])
    if not beats:
        raise RuntimeError("No beats in director JSON")

    clear_vse()

    # Characters setup (role-based; no backwards compatibility with name-based mapping)
    # Load role-to-prefix and genders from generator_inputs.json
    if not gen_inputs:
        raise RuntimeError(f"Failed to read generator inputs at {gen_inputs_path}")
    # Apply run settings (fps, resolution, engine) if present
    run_cfg = gen_inputs.get("run") or {}
    try:
        if "fps" in run_cfg:
            fps = int(run_cfg["fps"])
            scene.render.fps = fps
    except Exception:
        pass
    try:
        res = run_cfg.get("resolution") or {}
        rw = res.get("width"); rh = res.get("height")
        if rw and rh:
            scene.render.resolution_x = int(rw)
            scene.render.resolution_y = int(rh)
    except Exception:
        pass
    try:
        re = (run_cfg.get("render_engine") or "").upper()
        if re in ("BLENDER_EEVEE", "BLENDER_WORKBENCH"):
            scene.render.engine = re
    except Exception:
        pass

    # Re-apply quality preset if generator_inputs overrides the engine to Eevee after the initial pass.
    # (Without this, eevee settings can remain at whatever the .blend defaults were, e.g. 64 samples.)
    try:
        if scene.render.engine == "BLENDER_EEVEE":
            apply_quality_preset(scene, quality)
    except Exception:
        pass

    # Final render settings debug
    try:
        ee = scene.eevee
        print(
            f"[vpg] final render.engine={scene.render.engine} "
            f"cycles.samples={getattr(getattr(scene,'cycles',None),'samples',None)} "
            f"eevee.taa_render_samples={getattr(ee,'taa_render_samples',None)}",
            flush=True,
        )
    except Exception:
        print(f"[vpg] final render.engine={scene.render.engine}", flush=True)
    role_prefix_map = (gen_inputs.get("blender_mapping") or {}).get("role_prefix") or {}
    roles_conf = gen_inputs.get("characters") or {}
    if not roles_conf:
        raise RuntimeError("No roles defined in generator_inputs.json 'characters'")
    char_map = {}
    for role, conf in roles_conf.items():
        gender = ((conf.get("blender") or {}).get("gender")) or ""
        role_prefix = role_prefix_map.get(role, "")
        parts = _get_char_parts_for_role(role, role_prefix, gender)
        if not parts:
            raise RuntimeError(f"No mesh objects found for role '{role}' (prefix '{role_prefix}')")
        _for_each_part(parts, lambda o: clear_shape_key_animation(o))
        _for_each_part(parts, lambda o: zero_all_shapes(o, frame=1))
        char_map[role] = parts

    # All characters visible from the beginning; no scripted show/hide or fades

    # Lay audio strips and compute frame range
    total_end = 1
    if not CLI_NO_AUDIO:
        channel_for_char = {}
        next_free_channel = 1
        for b in beats:
            # Handle explicit pause beats (no audio, no visemes); extend timeline only
            if (b.get("type") or "").lower() == "pause" or not b.get("audio"):
                f_in = tc_to_frame(b["tc_in"], fps)
                try:
                    dur_f = int(round(float(b.get("duration", 1.0)) * fps))
                except Exception:
                    dur_f = int(round(1.0 * fps))
                total_end = max(total_end, f_in + dur_f + 2)
                continue
            # Normal spoken beat with audio/visemes
            char = b.get("char")
            if not char or char not in char_map:
                raise RuntimeError(f"Unknown or missing role '{char}' in beat")
            f_in = tc_to_frame(b["tc_in"], fps)
            if char not in channel_for_char:
                channel_for_char[char] = next_free_channel
                next_free_channel += 1
            ch = channel_for_char[char]
            snd = add_audio(b["audio"], frame_start=f_in, channel=ch)
            total_end = max(total_end, f_in + snd.frame_final_duration + 2)
    else:
        # Distributed render workers may not have audio locally; estimate timeline end from viseme times.
        end_s = estimate_timeline_end_seconds(beats)
        total_end = max(total_end, int(round(end_s * fps)) + 2)

    scene.frame_start = 1
    scene.frame_end = total_end
    # CLI overrides for chunked rendering
    try:
        if CLI_FRAME_START is not None:
            scene.frame_start = max(1, int(CLI_FRAME_START))
        if CLI_FRAME_END is not None:
            scene.frame_end = max(scene.frame_start, int(CLI_FRAME_END))
    except Exception:
        pass
    # Safety clamp to prevent runaway frame ranges.
    max_frame_end = CLI_MAX_FRAME_END
    if max_frame_end is None:
        try:
            max_frame_end = int(os.environ.get("VPG_MAX_FRAME_END", "12000"))
        except Exception:
            max_frame_end = 12000
    try:
        if max_frame_end and int(max_frame_end) > 0 and scene.frame_end > int(max_frame_end):
            print(f"[run_director_visemes] frame_end safety clamp: {scene.frame_end} -> {int(max_frame_end)}")
            scene.frame_end = int(max_frame_end)
    except Exception:
        pass

    # Apply visemes
    for b in beats:
        # Skip pause beats; they carry no visemes
        if (b.get("type") or "").lower() == "pause" or not b.get("visemes"):
            continue
        target_char = b.get("char")
        if not target_char or target_char not in char_map:
            raise RuntimeError(f"Unknown or missing role '{target_char}' in beat")
        parts = char_map[target_char]
        apply_visemes_multi(parts, b.get("visemes", []), fps)

    # Optional idle blinks
    if ENABLE_BLINKS:
        for parts in char_map.values():
            add_idle_blinks(parts, fps, start_f=scene.frame_start, end_f=scene.frame_end)

    # Optional: save a playback-ready .blend for realtime preview in the UI
    if CLI_PREPARE_VIEWPORT_BLEND:
        try:
            scene.sync_mode = 'AUDIO_SYNC'
        except Exception:
            pass
        # Try to make viewport lighter for playback
        try:
            sh = scene.display.shading
            sh.light = getattr(sh, "light", 'STUDIO')
            sh.color_type = getattr(sh, "color_type", 'MATERIAL')
        except Exception:
            pass
        preview_blend = Path(CLI_PREPARE_VIEWPORT_BLEND)
        preview_blend.parent.mkdir(parents=True, exist_ok=True)
        try:
            bpy.ops.wm.save_as_mainfile(filepath=str(preview_blend))
            print(f"[run_director_visemes] Saved playback-ready blend: {preview_blend}")
        except Exception as ex:
            print(f"[run_director_visemes] Warning: failed to save playback .blend: {ex}")

    # Allow skipping render entirely (e.g., when only preparing playback .blend)
    if CLI_NO_RENDER:
        return

    # Configure output path
    if output_frames:
        # Write frames to out/<stem>_frames/<stem>_####.png
        out_p = Path(outpath)
        frames_dir = out_p.parent / f"{out_p.stem}_frames"
        # Clean any existing PNG frames to avoid mixing old/new frames unless disabled
        if not CLI_NO_CLEAN_FRAMES:
            try:
                if frames_dir.exists():
                    for f in frames_dir.glob("*.png"):
                        try:
                            f.unlink()
                        except Exception:
                            pass
            except Exception:
                pass
        frames_dir.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(frames_dir / f"{out_p.stem}_####")
    else:
        scene.render.filepath = str(Path(outpath))
    # Optional: very fast Viewport Render Animation (requires running Blender with a UI, not -b)
    if CLI_VIEWPORT_RENDER:
        if getattr(bpy.app, "background", True):
            print("[run_director_visemes] Viewport render requested but Blender is running in background mode. Launch without -b to use viewport render.")
        else:
            # Use whichever operator is available for the current Blender version
            ok = False
            try:
                bpy.ops.render.opengl(animation=True)
                ok = True
            except Exception:
                pass
            if not ok:
                try:
                    bpy.ops.render.render('INVOKE_DEFAULT', animation=True, use_viewport=True)
                    ok = True
                except Exception:
                    pass
            if ok:
                return
            else:
                print("[run_director_visemes] Warning: Viewport render failed; falling back to normal render.")

    # Render (with a denoiser fallback for OptiX denoiser failures).
    try:
        bpy.ops.render.render(animation=True)
    except Exception as ex:
        msg = str(ex)
        # OptiX denoiser can fail in some headless/container setups even when OptiX rendering works.
        if "OptiX denoiser" in msg or "Failed to create OptiX denoiser" in msg:
            print("[cycles] OptiX denoiser failed; retrying with OPENIMAGEDENOISE (CPU) ...", flush=True)
            try:
                vl = bpy.context.view_layer
                if hasattr(vl, "cycles"):
                    try:
                        vl.cycles.use_denoising = True
                    except Exception:
                        pass
                    try:
                        if hasattr(vl.cycles, "denoiser"):
                            vl.cycles.denoiser = "OPENIMAGEDENOISE"
                    except Exception:
                        pass
                # Some versions store denoiser on scene.cycles
                try:
                    if hasattr(scene.cycles, "denoiser"):
                        scene.cycles.denoiser = "OPENIMAGEDENOISE"
                except Exception:
                    pass
                bpy.ops.render.render(animation=True)
                return
            except Exception:
                print("[cycles] OIDN retry failed; retrying with denoising OFF ...", flush=True)
                try:
                    vl = bpy.context.view_layer
                    if hasattr(vl, "cycles"):
                        try:
                            vl.cycles.use_denoising = False
                        except Exception:
                            pass
                    bpy.ops.render.render(animation=True)
                    return
                except Exception:
                    pass
        raise

if __name__ == "__main__":
    import sys as _sys, argparse as _argparse
    argv = _sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    ap = _argparse.ArgumentParser()
    ap.add_argument("--director", required=True)
    ap.add_argument("--out", help="Output video path; defaults to project out/demo*.mp4")
    ap.add_argument("--lead_frames", type=int)
    ap.add_argument("--time_offset_sec", type=float)
    ap.add_argument("--smooth_factor", type=float)
    ap.add_argument("--quality", choices=["fast","full"], help="Override render quality preset")
    ap.add_argument("--engine", choices=["eevee","workbench","cycles"], help="Render engine override")
    ap.add_argument("--prepare_viewport_blend", help="Path to save a playback-ready .blend (visemes keyed, audio laid out).")
    ap.add_argument("--viewport_render", action="store_true", help="Use Viewport Render Animation (UI mode only; much faster).")
    ap.add_argument("--no_render", action="store_true", help="Prepare scene (and optional .blend) but do not render.")
    ap.add_argument("--frames", action="store_true", help="Force PNG image sequence output (even if opaque).")
    ap.add_argument("--transparent", action="store_true", help="Enable Film Transparent and render PNG RGBA frames (alpha-friendly).")
    ap.add_argument("--opaque", action="store_true", help="Disable Film Transparent (opaque background) for easier debugging.")
    ap.add_argument("--frame_start", type=int, help="Override scene.frame_start (chunked render).")
    ap.add_argument("--frame_end", type=int, help="Override scene.frame_end (chunked render).")
    ap.add_argument("--max_frame_end", type=int, default=None, help="Safety clamp for scene.frame_end (defaults to env VPG_MAX_FRAME_END or 12000).")
    ap.add_argument("--no_clean_frames", action="store_true", help="Do not delete existing frames in the output frames directory.")
    ap.add_argument("--no_audio", action="store_true", help="Do not load audio into VSE; estimate timeline end from visemes instead (useful for distributed renders).")
    args = ap.parse_args(argv)
    if not args.out:
        project_root = Path(__file__).resolve().parent.parent
        out_dir = project_root / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        base = "demo_visemes"; ext = ".mp4"; i = 0
        while True:
            suffix = "" if i == 0 else str(i)
            candidate = out_dir / f"{base}{suffix}{ext}"
            if not candidate.exists():
                args.out = str(candidate)
                print(f"[run_director_visemes] Using output: {args.out}")
                break
            i += 1
    CLI_LEAD_FRAMES = args.lead_frames
    CLI_TIME_OFFSET_SEC = args.time_offset_sec
    CLI_SMOOTH_FACTOR = args.smooth_factor
    CLI_ENGINE = args.engine
    CLI_PREPARE_VIEWPORT_BLEND = args.prepare_viewport_blend
    CLI_VIEWPORT_RENDER = bool(args.viewport_render)
    CLI_NO_RENDER = bool(args.no_render)
    CLI_FRAMES = bool(args.frames)
    CLI_TRANSPARENT = bool(args.transparent)
    CLI_OPAQUE = bool(args.opaque)
    CLI_FRAME_START = args.frame_start
    CLI_FRAME_END = args.frame_end
    CLI_MAX_FRAME_END = args.max_frame_end
    CLI_NO_CLEAN_FRAMES = bool(args.no_clean_frames)
    CLI_NO_AUDIO = bool(args.no_audio)

    # If CLI quality provided, inject into director JSON at runtime
    if args.quality:
        try:
            data = json.loads(Path(args.director).read_text())
            data.setdefault("render", {})["quality"] = args.quality
            tmp = Path(args.director).with_suffix(".tmp.json")
            tmp.write_text(json.dumps(data))
            main(str(tmp), args.out)
            try:
                tmp.unlink()
            except Exception:
                pass
        except Exception:
            main(args.director, args.out)
    else:
        main(args.director, args.out)


