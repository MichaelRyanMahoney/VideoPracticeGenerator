import sys
from pathlib import Path

import bpy


def main():
    argv = sys.argv
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    roles = ["Disputant1", "MediatorA", "MediatorB", "Disputant2"]
    if "--roles" in args:
        i = args.index("--roles")
        roles = args[i + 1 :]
    missing = []
    for r in roles:
        if bpy.data.collections.get(r) is None:
            missing.append(r)
    if missing:
        raise SystemExit(f"Missing role collections in scene: {missing}")
    print("[OK] Role collections present:", roles)


if __name__ == "__main__":
    main()

