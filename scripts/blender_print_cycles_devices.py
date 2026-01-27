#!/usr/bin/env python3
"""
Print Cycles device discovery + current scene Cycles device mode.

Run:
  blender -b /path/to/any.blend --python scripts/blender_print_cycles_devices.py
"""
import sys

try:
    import bpy  # type: ignore
except Exception:
    print("This script must be run inside Blender (bpy not found).", file=sys.stderr)
    raise SystemExit(1)


def _safe_get_cycles_prefs():
    try:
        prefs = bpy.context.preferences
        return prefs.addons["cycles"].preferences
    except Exception:
        return None


def _fmt_devices(cprefs):
    out = []
    try:
        for d in getattr(cprefs, "devices", []) or []:
            out.append(
                {
                    "name": getattr(d, "name", ""),
                    "type": getattr(d, "type", ""),
                    "use": bool(getattr(d, "use", False)),
                }
            )
    except Exception:
        pass
    return out


def main() -> int:
    scene = bpy.context.scene
    cprefs = _safe_get_cycles_prefs()
    if not cprefs:
        print("[cycles] ERROR: cycles addon preferences not available")
        return 2

    # Try both backends; just report what happens.
    for backend in ("OPTIX", "CUDA"):
        try:
            cprefs.compute_device_type = backend
        except Exception as ex:
            print(f"[cycles] backend={backend} set compute_device_type failed: {ex}")
            continue
        try:
            cprefs.get_devices()
        except Exception as ex:
            print(f"[cycles] backend={backend} get_devices failed: {ex}")
        print(f"[cycles] backend={backend} compute_device_type={getattr(cprefs, 'compute_device_type', None)} devices={_fmt_devices(cprefs)}")

    # Current scene settings
    try:
        print(f"[scene] render.engine={scene.render.engine}")
    except Exception:
        pass
    try:
        print(f"[scene] cycles.device={scene.cycles.device}")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

