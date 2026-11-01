"""Explicit local media/voice acceptance; emit counts, never audio or transcript."""
from collections import Counter
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from wechat_reader import runtime
from wechat_reader.archive import Archive
from wechat_reader.cli import execute, parser
from wechat_reader.keys import load_keys
from wechat_reader.media import MediaResolver
from wechat_reader.source import Source


def main():
    cfg = runtime.config()
    source = Source(Path(cfg["source"]), Path(cfg["key_file"]) if cfg.get("key_file") else None)
    keys = load_keys(source.key_file)
    resolver = MediaResolver(source.root, runtime.DATA / "media-cache", image_key=keys.get("img_aes"), image_xor=keys.get("img_xor"))
    report = {"validation": "real_local_media_and_voice", "media": {}}
    with Archive(runtime.DATA / "archive.db", readonly=True) as archive:
        for kind in ("image", "video", "file"):
            limit = 100000 if kind in ("file", "video") else 20
            rows = archive.db.execute("SELECT v.payload FROM messages m JOIN versions v ON m.last_seq=v.seq WHERE m.account=? AND m.kind=? ORDER BY m.ts DESC,m.id DESC LIMIT ?", (source.account, kind, limit)).fetchall()
            states = Counter()
            variants = Counter()
            source_media_missing = 0
            read_sample = None
            for row in rows:
                message = json.loads(row[0])
                result = resolver.resolve(message)
                states[result["state"]+":"+result["detail"]] += 1
                source_media_missing += bool(result.get("source_media_missing"))
                if result["state"] == "available":
                    variants[result.get("variant", "unknown")] += 1
                if result["state"] == "available" and (read_sample is None or (read_sample.get("variant") != "original" and result.get("variant") == "original")):
                    read_result, data = resolver.read(message)
                    read_sample = {"state": read_result["state"], "bytes": len(data) if data is not None else 0, "variant": read_result.get("variant"), "media_kind": read_result.get("media_kind"), "format": read_result.get("format")}
            report["media"][kind] = {"sampled": len(rows), "states": dict(states), "variants": dict(variants), "source_media_missing": source_media_missing, "read_sample": read_sample}
        voice_id = archive.db.execute("SELECT id FROM messages WHERE account=? AND kind='voice' AND recalled=0 ORDER BY ts,id LIMIT 1", (source.account,)).fetchone()[0]
    enqueue = execute(parser().parse_args(["voice", "enqueue", "--id", str(voice_id)]))
    run = execute(parser().parse_args(["voice", "run", "--id", str(voice_id), "--model", str(runtime.DATA / "models/faster-whisper-tiny")]))
    job = run["job"] or enqueue["job"]
    report["voice"] = {"state": job["state"], "detail": job["detail"], "transcript_characters": len(job["transcript"])}
    runtime.write_json(runtime.DATA / "local-media-acceptance.json", report)
    runtime.write_json(runtime.DATA / "acceptance" / ("media-"+str(time.time_ns())+".json"), report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if report["voice"]["state"] != "done":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
