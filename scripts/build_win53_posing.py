"""Build the opt-in, source-only Win53 posing add-on from reviewed inputs."""

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
EDITION = "0.42.3-win53-phase3"
HOST = {"blender": "5.3.0", "build": "02f0899d78e3", "offline": True}
INVENTORY = ROOT / "win53-posing" / "SOURCES.json"
SOURCE_ROOTS = ("rigging_workflow/", "cli-v003/rigging_workflow/", "win53-posing/overlay/")
ALLOWED_NAMES = {"LICENSE", "THIRD_PARTY_NOTICES.md", "vendor/pycpd/LICENSE", "volume/LICENSE"}
ALLOWED_SUFFIXES = {".py", ".cu"}
WORKSTATION = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[/\\]+|/(?:home|Users)/[A-Za-z0-9_.-]+/")
SECRET = re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|"
                    r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|"
                    r"xox[baprs]-[A-Za-z0-9-]{12,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def member_path(name):
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or path.as_posix() != name or ".." in path.parts
            or "\\" in name or ":" in name):
        raise ValueError(f"Unsafe relative member: {name!r}")
    return path


def source_bytes(member, entry):
    relative = entry["source"]
    member_path(relative)
    if not relative.startswith(SOURCE_ROOTS):
        raise ValueError(f"Unreviewed source root: {relative}")
    candidate = ROOT / relative
    if candidate.resolve(strict=True) != candidate or not candidate.is_file():
        raise ValueError(f"Source redirect or missing file: {relative}")
    data = candidate.read_bytes()
    if digest(data) != entry["sha256"]:
        raise ValueError(f"Source bytes differ from the reviewed inventory: {relative}")
    text = data.decode("utf-8-sig")
    if SECRET.search(text) or WORKSTATION.search(text):
        raise ValueError(f"Potential private source content: {relative}")
    if member.endswith(".py"):
        ast.parse(text, filename=relative)
    return data


def assemble():
    source_map = json.loads(INVENTORY.read_text(encoding="utf-8"))
    if source_map["edition"] != EDITION or source_map["host"] != HOST:
        raise ValueError("The source inventory is for a different edition or host")
    entries = {}
    for name, source in source_map["files"].items():
        path = member_path(name)
        if path.suffix.lower() not in ALLOWED_SUFFIXES and name not in ALLOWED_NAMES:
            raise ValueError(f"Non-source payload: {name}")
        entries[name] = source_bytes(name, source)
    if not ALLOWED_NAMES <= entries.keys() or "__init__.py" not in entries:
        raise ValueError("Required add-on and license files are absent")
    entries["SOURCE-MAP.json"] = json_bytes(source_map)
    entries["exchange-runtime-manifest.json"] = json_bytes({
        "schema": "c94.exchange-runtime.package.v1",
        "files": {name: digest(data) for name, data in entries.items()},
        "source_map_sha256": digest(entries["SOURCE-MAP.json"]),
        "host": HOST,
        "scope": "Asset-free Win53 posing runtime",
    })
    return {f"rigging_workflow/{name}": data for name, data in sorted(entries.items())}


def build(output):
    entries = assemble()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as bundle:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
    with zipfile.ZipFile(output) as bundle:
        if bundle.namelist() != list(entries) or bundle.testzip() is not None:
            raise ValueError("ZIP member inventory or CRC differs")
        for name, data in entries.items():
            if bundle.read(name) != data:
                raise ValueError(f"ZIP member bytes differ: {name}")
    return {"edition": EDITION, "file": str(output), "sha256": digest(output.read_bytes()),
            "bytes": output.stat().st_size, "members": len(entries), "host": HOST}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New ZIP path")
    print(json.dumps(build(parser.parse_args().output.resolve()), indent=2))
