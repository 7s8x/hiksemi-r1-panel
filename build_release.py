"""Build an allowlisted source distribution without reading host configuration."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent


def build(destination):
    target = Path(destination).resolve()
    if target.exists() or target == ROOT or ROOT in target.parents:
        raise ValueError("use a new output directory outside the source tree")
    names = json.loads((ROOT / "release-files.json").read_text())
    if not isinstance(names, list) or len(names) != len(set(names)):
        raise ValueError("invalid release allowlist")
    payload = []
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe release path")
        file = ROOT / relative
        if file.is_symlink() or not file.is_file() or ROOT not in file.resolve().parents:
            raise ValueError("release file missing or not an ordinary project file")
        payload.append((name, file))
    target.mkdir(parents=True)
    checksums = []
    for name, file in payload:
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, output)
        checksums.append(hashlib.sha256(output.read_bytes()).hexdigest() + "  " + name)
    (target / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n")
    return len(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination")
    print(f"release_files={build(parser.parse_args().destination)}")
