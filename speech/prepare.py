"""Download a versioned official model and compile keywords, never touch robotd."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent


def digest(path):
    # file_digest was added in Python 3.11. Keep transfer/integrity checks
    # usable with older host interpreters without reading models into RAM.
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def keyword_cli():
    # On Windows python.exe can live above Scripts (e.g. base Anaconda).
    # Resolve this interpreter's scripts dir, never another environment on PATH.
    name = "sherpa-onnx-cli.exe" if sys.platform == "win32" else "sherpa-onnx-cli"
    cli = Path(sysconfig.get_path("scripts")) / name
    if not cli.is_file():
        raise RuntimeError(f"Missing {cli}; install speech requirements with the intended virtualenv Python")
    return cli


def catalog(model_id=None):
    data = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    model_id = model_id or data["default"]
    if model_id not in data["models"]:
        raise ValueError(f"Unknown model ID: {model_id}")
    return model_id, data["models"][model_id]


def verify(model_dir, model_id, spec):
    manifest = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["model_id"] != model_id or manifest["source_url"] != spec["url"]:
        raise ValueError("Model identity/source mismatch")
    required = [spec[key] for key in ("encoder", "decoder", "joiner", "tokens", "lexicon")]
    required += ["keywords.txt", "keywords_raw.txt"]
    for name in required:
        if manifest["files"].get(name) != digest(model_dir / name):
            raise ValueError(f"SHA256 mismatch: {name}; prepare again before recognition")
    return manifest


def extract_selected(archive, destination, names):
    # Never extractall: ignore paths/links supplied by the tar archive.
    with tarfile.open(archive, "r:bz2") as bundle:
        members = {m.name: m for m in bundle.getmembers()}
        for name in names:
            if Path(name).name != name:
                raise ValueError("Catalog file names must be basenames")
            candidates = [m for key, m in members.items() if key.endswith("/" + name)]
            if len(candidates) != 1 or not candidates[0].isfile() or candidates[0].size > 100_000_000:
                raise ValueError(f"Invalid/missing archive member: {name}")
            with bundle.extractfile(candidates[0]) as source, (destination / name).open("wb") as target:
                shutil.copyfileobj(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id")
    parser.add_argument("--archive", type=Path, help="Existing official tar.bz2, for offline setup")
    args = parser.parse_args()
    model_id, spec = catalog(args.model_id)
    destination = ROOT / "models" / model_id
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "manifest.json").is_file():
        try:
            verify(destination, model_id, spec)
            if (destination / "keywords_raw.txt").read_bytes() == (ROOT / "keywords_raw.txt").read_bytes():
                print(f"Already prepared and verified: {destination}")
                return
        except (OSError, ValueError, KeyError):
            pass  # Repair damaged/incomplete contents from the official archive.
    archives = ROOT / "downloads"
    archives.mkdir(exist_ok=True)
    archive = args.archive or archives / (spec["name"] + ".tar.bz2")
    if not archive.is_file():
        if args.archive:
            raise FileNotFoundError(archive)
        print(f"Downloading official model: {spec['url']}", flush=True)
        temporary = archive.with_suffix(".part")
        with urllib.request.urlopen(spec["url"], timeout=45) as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
        temporary.replace(archive)
    names = [spec[k] for k in ("encoder", "decoder", "joiner", "tokens", "lexicon")]
    # Stage complete contents first: interrupted setup must not leave a valid manifest.
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=destination.parent) as directory:
        stage = Path(directory)
        extract_selected(archive, stage, names)
        shutil.copyfile(ROOT / "keywords_raw.txt", stage / "keywords_raw.txt")
        cli = keyword_cli()
        subprocess.run([str(cli), "text2token", "--tokens", str(stage / spec["tokens"]),
                        "--tokens-type", "phone+ppinyin", "--lexicon", str(stage / spec["lexicon"]),
                        str(stage / "keywords_raw.txt"), str(stage / "keywords.txt")], check=True)
        names += ["keywords_raw.txt", "keywords.txt"]
        manifest = {"model_id": model_id, "source_url": spec["url"],
                    "archive_sha256": digest(archive),
                    "hash_trust": "locally recorded, not an upstream published checksum",
                    "files": {name: digest(stage / name) for name in names}}
        # Invalidate the old manifest before replacing individual model files.
        (destination / "manifest.json").unlink(missing_ok=True)
        for name in names:
            (stage / name).replace(destination / name)
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    verify(destination, model_id, spec)
    print(f"Prepared and SHA256 recorded: {destination}")


if __name__ == "__main__":
    main()
