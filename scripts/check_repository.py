"""Tracked/staged artifact and known-local-secret check; never print secret values."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main():
    paths = [p.decode("utf-8") for p in git("ls-files", "-z").split(b"\0") if p]
    paths += [p.decode("utf-8") for p in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p]
    paths = sorted(set(paths))
    secret_values = []
    cfg_path = ROOT / "data/config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        secret_values += [cfg.get("account", ""), cfg.get("source", "")]
        if cfg.get("key_file"):
            from wechat_reader.keys import load_keys
            keys = load_keys(Path(cfg["key_file"]))
            secret_values += [str(keys.get(k, "")) for k in ("db", "img_aes")]
    tokens = [v.encode("utf-8") for v in secret_values if len(v) >= 8]
    findings = []
    for name in paths:
        path = ROOT / name
        if not path.is_file():
            continue
        if name.startswith(("data/", ".venv/")) or path.suffix.lower() in (".db", ".bin", ".sqlite", ".log", ".silk", ".pcm"):
            findings.append({"file": name, "reason": "private_artifact_extension"})
            continue
        content = path.read_bytes()
        if any(value in content for value in tokens) or re.search(rb"(?:github_pat_|ghp_)[A-Za-z0-9_]{20,}", content):
            findings.append({"file": name, "reason": "known_private_value_or_credential"})
    ancestor = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", "4a640508e23407642670604fce54bcc63a8a83ff", "HEAD"], capture_output=True).returncode == 0
    result = dict(ok=not findings and ancestor, checked_files=len(paths), findings=findings, original_history_preserved=ancestor)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
