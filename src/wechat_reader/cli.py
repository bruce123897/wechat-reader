"""Structured CLI. Diagnostics avoid private paths, content and exception text."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .archive import Archive
from . import runtime


def timestamp(value):
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("ISO_time_requires_explicit_timezone")
        return int(dt.timestamp())


def parser():
    p = argparse.ArgumentParser(prog="wechat-reader", description="Wechat Reader：只读源数据，独立归档；默认 JSON 输出")
    p.add_argument("--db", type=Path, default=runtime.DATA / "archive.db")
    sub = p.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="导入 JSON 批次（合成测试与真实源分开）")
    imp.add_argument("file", type=Path)
    sub.add_parser("status")
    doctor = sub.add_parser("doctor"); doctor.add_argument("--max-age", type=int); doctor.add_argument("--source", action="store_true", help="额外验证当前源及密钥")
    for name in ("search", "read", "export"):
        q = sub.add_parser(name)
        if name == "search":
            q.add_argument("keyword")
        q.add_argument("--account")
        q.add_argument("--chat")
        q.add_argument("--sender")
        q.add_argument("--kind")
        q.add_argument("--since", type=timestamp)
        q.add_argument("--until", type=timestamp)
        q.add_argument("--limit", type=int, default=50)
        q.add_argument("--cursor")
        if name == "export":
            q.add_argument("--format", choices=["json", "csv", "md"], default="json")
            q.add_argument("--output", required=True, type=Path)
    m = sub.add_parser("msg"); m.add_argument("id", type=int)
    c = sub.add_parser("context"); c.add_argument("id", type=int); c.add_argument("--radius", type=int, default=2)
    find = sub.add_parser("find"); find.add_argument("term", nargs="?", default=""); find.add_argument("--account")
    inbox = sub.add_parser("inbox"); inbox.add_argument("--account"); inbox.add_argument("--unread", action="store_true")
    members = sub.add_parser("members"); members.add_argument("room"); members.add_argument("--account", required=True)
    new = sub.add_parser("new"); new.add_argument("--account", required=True); new.add_argument("--reader", default="default"); new.add_argument("--limit", type=int, default=100); new.add_argument("--consume", action="store_true")
    sub.add_parser("discover")
    cfg = sub.add_parser("configure"); cfg.add_argument("--source", type=Path, required=True); cfg.add_argument("--key-file", type=Path)
    sub.add_parser("probe")
    sync = sub.add_parser("sync"); sync.add_argument("--full", action="store_true", help="重读所有分片并重解析，仍幂等去重")
    daemon = sub.add_parser("daemon"); daemon.add_argument("action", choices=["start", "stop", "status", "run"]); daemon.add_argument("--interval", type=float, default=20)
    media = sub.add_parser("media"); media.add_argument("id", type=int); media.add_argument("--copy", action="store_true")
    voice = sub.add_parser("voice"); voice.add_argument("action", choices=["enqueue", "run", "work", "status", "retry"]); voice.add_argument("--id", type=int); voice.add_argument("--model", type=Path); voice.add_argument("--limit", type=int, default=10)
    return p


def source_from_config():
    from .source import Source
    cfg = runtime.config()
    return Source(Path(cfg["source"]), Path(cfg["key_file"]) if cfg.get("key_file") else None)


def export(archive, args):
    path = runtime.output_path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {k: getattr(args, k, None) for k in ("account", "chat", "sender", "kind", "since", "until")}
    if fields["chat"] and fields["account"]:
        fields["chat"] = archive.resolve(fields["account"], fields["chat"])
    rows, cursor = [], args.cursor
    while True:
        page = archive.query(**fields, limit=1000, cursor=cursor)
        rows.extend(page["messages"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    if args.format == "json":
        content = json.dumps(rows, ensure_ascii=False, indent=2)
    elif args.format == "csv":
        out = io.StringIO(newline="")
        cols = ["id", "account", "chat", "ts", "sender", "kind", "text", "recalled"]
        writer = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        # Escape spreadsheet formulas in content and identities.
        for row in rows:
            writer.writerow({k: ("'"+v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v) for k, v in row.items()})
        content = out.getvalue()
    else:
        content = "# Wechat Reader 导出\n\n" + "\n\n".join(f"{datetime.fromtimestamp(m['ts'], timezone.utc).isoformat()} {m['sender']} [{m['kind']}]" + (" [已撤回]" if m["recalled"] else "") + "\n\n" + m["text"] for m in rows)
    tmp = path.with_suffix(path.suffix+".tmp")
    tmp.write_text(content, encoding="utf-8"); tmp.replace(path)
    return {"exported": len(rows), "output": str(path), "format": args.format}


def execute(args):
    cmd = args.command
    path = runtime.output_path(args.db)
    if cmd == "discover":
        from .source import discover
        return {"sources": [str(p) for p in discover()]}
    if cmd == "configure":
        from .source import Source
        source = Source(args.source, args.key_file)
        # Verify a small source database before persisting a configuration.
        from .source import capture
        raw, wal, _ = capture(source.storage / "contact/contact.db")
        connection = source._open(raw, wal)
        connection.close()
        key_file = None
        if source.key_file.exists():
            key_file = runtime.DATA / "keys" / (source.account + ".bin")
            key_file = runtime.output_path(key_file)
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(source.key_file.read_bytes())
        cfg = dict(source=str(args.source.resolve()), account=source.account, key_file=str(key_file) if key_file else None)
        runtime.write_json(runtime.DATA / "config.json", cfg)
        return {"configured": True, "account_count": 1}
    if cmd == "probe":
        return source_from_config().probe()
    if cmd == "daemon":
        if not .5 <= args.interval <= 86400:
            raise ValueError("interval_must_be_between_half_second_and_one_day")
        if args.action == "status":
            return runtime.daemon_state()
        if args.action == "stop":
            running = runtime.daemon_state().get("running")
            if running:
                (runtime.DATA / "daemon.stop").touch()
            return {"stop_requested": bool(running)}
        runtime.config()
        if args.action == "start":
            return runtime.start_daemon(path, args.interval)
        runtime.run_daemon(path, args.interval)
        return {"stopped": True}
    readonly = cmd not in ("import", "sync", "voice") and not (cmd == "new" and args.consume)
    with Archive(path, readonly=readonly) as archive:
        if cmd == "import":
            data = json.loads(args.file.read_text(encoding="utf-8-sig"))
            batches = data if isinstance(data, list) else [data]
            return {"batches": [archive.ingest(b) for b in batches]}
        if cmd in ("search", "read"):
            fields = {k: getattr(args, k, None) for k in ("account", "chat", "sender", "kind", "since", "until", "keyword", "limit", "cursor")}
            if fields["chat"] and fields["account"]:
                fields["chat"] = archive.resolve(fields["account"], fields["chat"])
            return archive.query(**fields)
        if cmd == "msg":
            return archive.message(args.id)
        if cmd == "context":
            return archive.context(args.id, args.radius)
        if cmd == "find":
            return {"contacts": archive.find(args.term, args.account)}
        if cmd == "inbox":
            return {"sessions": archive.inbox(args.account, args.unread)}
        if cmd == "members":
            room = archive.resolve(args.account, args.room)
            return {"members": archive.members(args.account, room)}
        if cmd == "new":
            return archive.new(args.account, args.reader, args.limit, args.consume)
        if cmd == "export":
            return export(archive, args)
        if cmd == "status":
            return dict(archive.status(), daemon=runtime.daemon_state())
        if cmd == "doctor":
            result = archive.doctor(max_age=args.max_age)
            if args.source:
                try:
                    result["source"] = source_from_config().validate()
                except Exception as exc:
                    result.update(ok=False, source_error=type(exc).__name__)
            daemon = runtime.daemon_state()
            if daemon.get("running") and daemon.get("error"):
                result.update(ok=False, daemon_error=daemon["error"])
            return result
        if cmd == "sync":
            try:
                return runtime.sync_once(archive, source_from_config(), full=args.full)
            except Exception as exc:
                cfg = runtime.config()
                archive.failure(cfg.get("account", "configured"), "__sync__", type(exc).__name__)
                raise
        if cmd in ("media", "voice"):
            return media_voice(archive, args)
    raise ValueError("unknown_command")


def media_voice(archive, args):
    from .media import MediaResolver
    from .voice import FasterWhisperTranscriber, VoiceQueue
    queue = VoiceQueue(archive.path.with_name(archive.path.stem + ".voices.db")) if args.command == "voice" else None
    if args.command == "voice" and args.action == "status":
        return {"jobs": queue.status(), "job": queue.get(args.id) if args.id else None}
    if args.command == "voice" and args.action == "retry":
        return {"retried": queue.retry(args.id)}
    if args.command == "voice" and args.action in ("run", "work"):
        transcriber = FasterWhisperTranscriber(args.model, cache_dir=runtime.DATA / "audio-cache")
        source = source_from_config()

        def loader(message_id):
            message = archive.message(int(message_id))
            if message["account"] != source.account or message["kind"] != "voice" or message["recalled"]:
                return None
            return cache_voice(source, message)

        if args.action == "run":
            return {"job": queue.run_once(transcriber, media_loader=loader, message_id=args.id)}
        if not 1 <= args.limit <= 10000:
            raise ValueError("worker_limit_must_be_1_to_10000")
        counts = {}
        for _ in range(args.limit):
            job = queue.run_once(transcriber, media_loader=loader)
            if job is None:
                break
            counts[job["state"]] = counts.get(job["state"], 0)+1
        return {"processed": sum(counts.values()), "states": counts}
    if args.id is None:
        raise ValueError("message_id_required")
    message = archive.message(args.id)
    cfg = runtime.config()
    if message["account"] != cfg["account"]:
        raise ValueError("configure_source_for_message_account_first")
    keys = {}
    if cfg.get("key_file"):
        from .keys import load_keys
        keys = load_keys(Path(cfg["key_file"]))
    resolver = MediaResolver(Path(cfg["source"]), runtime.DATA / "media-cache",
                             image_key=keys.get("img_aes"), image_xor=keys.get("img_xor"))
    if args.command == "media":
        result = resolver.resolve(message)
        if args.copy and result["state"] == "available":
            result, content = resolver.read(message)
            if content is not None:
                target = runtime.DATA / "media-cache" / (hashlib.sha256(content).hexdigest()+Path(result["path"]).suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                result.update(path=str(target), detail="copied_to_own_cache")
        return result
    if message["kind"] != "voice" or message["recalled"]:
        raise ValueError("message_is_not_voice")
    result = resolver.resolve(message)
    path = result.get("path") if result["state"] == "available" else None
    if path is None and message["server_id"] not in ("", "0"):
        path = cache_voice(source_from_config(), message)
    return {"job": queue.enqueue(message["id"], path)}


def cache_voice(source, message):
    if message["server_id"] in ("", "0"):
        return None
    audio = source.read_voice(message["server_id"], message["chat"])
    if not audio:
        return None
    target = runtime.DATA / "voice-cache" / (hashlib.sha256(audio).hexdigest()+".silk")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(audio)
        try:
            # Publish an immutable blob without replacing a file another worker
            # may already be reading (Windows denies that replacement).
            os.link(tmp, target)
        except FileExistsError:
            if hashlib.sha256(target.read_bytes()).hexdigest() != target.stem:
                raise ValueError("voice_cache_content_mismatch")
    finally:
        tmp.unlink(missing_ok=True)
    return target


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        code = 2 if isinstance(result, dict) and result.get("ok") is False else 0
    except Exception as exc:
        # Raw exception strings may include private SQL, content, paths or key bytes.
        result, code = {"ok": False, "error": type(exc).__name__, "detail": "operation_failed_check_configuration_and_doctor"}, 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return code
