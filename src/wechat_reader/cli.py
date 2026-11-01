"""Structured CLI. Diagnostics avoid private paths, content and exception text."""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
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
    doctor = sub.add_parser("doctor"); doctor.add_argument("--max-age", type=int)
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
    sub.add_parser("sync")
    daemon = sub.add_parser("daemon"); daemon.add_argument("action", choices=["start", "stop", "status", "run"]); daemon.add_argument("--interval", type=float, default=20)
    media = sub.add_parser("media"); media.add_argument("id", type=int); media.add_argument("--copy", action="store_true")
    voice = sub.add_parser("voice"); voice.add_argument("action", choices=["enqueue", "run", "status", "retry"]); voice.add_argument("--id", type=int); voice.add_argument("--model", type=Path)
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
        cfg = dict(source=str(args.source.resolve()), account=source.account, key_file=str(args.key_file.resolve()) if args.key_file else None)
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
            daemon = runtime.daemon_state()
            if daemon.get("running") and daemon.get("error"):
                result.update(ok=False, daemon_error=daemon["error"])
            return result
        if cmd == "sync":
            try:
                return runtime.sync_once(archive, source_from_config())
            except Exception as exc:
                cfg = runtime.config()
                archive.failure(cfg.get("account", "configured"), "__sync__", type(exc).__name__)
                raise
        if cmd in ("media", "voice"):
            return media_voice(archive, args)
    raise ValueError("unknown_command")


def media_voice(archive, args):
    raise ValueError("media_voice_integration_pending")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        code = 2 if isinstance(result, dict) and result.get("ok") is False else 0
    except (OSError, ValueError, RuntimeError, ImportError, KeyError) as exc:
        # Raw exception strings may include private SQL, content, paths or key bytes.
        result, code = {"ok": False, "error": type(exc).__name__, "detail": "operation_failed_check_configuration_and_doctor"}, 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return code
