"""Explicit public model download; no account credentials or private audio uploaded."""
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODEL = "Systran/faster-whisper-tiny"
REVISION = "d90ca5fe260221311c53c58e660288d3deb8d356"
TARGET = ROOT / "data/models/faster-whisper-tiny"


def main():
    manifest_path = TARGET / "manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("revision") == REVISION and previous.get("files") and all((TARGET/name).is_file() and hashlib.sha256((TARGET/name).read_bytes()).hexdigest() == digest for name, digest in previous["files"].items()):
            print(json.dumps(dict(verified=len(previous["files"]), revision=REVISION)))
            return
    with urllib.request.urlopen("https://huggingface.co/api/models/" + MODEL + "/revision/" + REVISION, timeout=30) as response:
        metadata = json.load(response)
    revision = metadata["sha"]
    if revision != REVISION:
        raise RuntimeError("model_revision_mismatch")
    files = [r["rfilename"] for r in metadata["siblings"] if r["rfilename"] in {"model.bin", "config.json", "tokenizer.json", "vocabulary.json", "vocabulary.txt", "preprocessor_config.json"}]
    TARGET.mkdir(parents=True, exist_ok=True)
    manifest = dict(model=MODEL, revision=revision, files={})
    for filename in files:
        path = TARGET / filename
        tmp = path.with_suffix(path.suffix + ".download")
        with urllib.request.urlopen(f"https://huggingface.co/{MODEL}/resolve/{revision}/{filename}", timeout=60) as response, tmp.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        tmp.replace(path)
        manifest["files"][filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    (TARGET / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(dict(downloaded=len(files), revision=revision, bytes=sum((TARGET/f).stat().st_size for f in files))))


if __name__ == "__main__":
    main()
