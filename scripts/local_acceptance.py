"""Opt-in actual local validation. Output is counts only; never private content.

This reads authorized local source/key files and writes only this checkout/data.
It does not read an old archive or invoke another reader.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from wechat_reader.archive import Archive
from wechat_reader import runtime
from wechat_reader.cli import execute, parser as cli_parser
from wechat_reader.source import Source, capture, discover


def setup():
    valid, failures = [], 0
    for root in discover():
        try:
            source = Source(root)
            database, wal, _ = capture(source.storage / "contact/contact.db")
            connection = source._open(database, wal)
            connection.close()
            valid.append(source)
        except Exception:
            failures += 1
    if len(valid) != 1:
        raise RuntimeError("automatic_setup_requires_exactly_one_unlockable_account")
    source = valid[0]
    execute(cli_parser().parse_args(["configure", "--source", str(source.root)]))
    return dict(discovered=len(valid)+failures, unlockable=len(valid), unavailable=failures)


def fingerprints(source):
    values = {}
    for path in source._paths():
        for candidate in (path, path.with_name(path.name + "-wal")):
            if candidate.exists():
                values[candidate] = hashlib.sha256(candidate.read_bytes()).digest()
    return values


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--setup", action="store_true")
    args = p.parse_args()
    report = {"validation": "real_local_readonly", "time": time.time()}
    if args.setup:
        report["discovery"] = setup()
    cfg = runtime.config()
    source = Source(Path(cfg["source"]), Path(cfg["key_file"]) if cfg.get("key_file") else None)
    before = fingerprints(source)
    with Archive(runtime.DATA / "archive.db") as archive:
        started = time.monotonic()
        report["first_sync"] = runtime.sync_once(archive, source)
        report["sync_seconds"] = round(time.monotonic()-started, 3)
        report["repeat_sync"] = runtime.sync_once(archive, source)
        status = archive.status()
        report["archive"] = {k: status[k] for k in ("messages", "versions", "contacts", "members")}
        page = archive.query(account=source.account, limit=2)
        report["query"] = {"first_page": len(page["messages"]), "has_next": bool(page["next_cursor"])}
        if page["next_cursor"]:
            second = archive.query(account=source.account, limit=2, cursor=page["next_cursor"])
            report["query"]["pages_disjoint"] = not ({m["id"] for m in page["messages"]} & {m["id"] for m in second["messages"]})
        if page["messages"]:
            report["context_count"] = len(archive.context(page["messages"][0]["id"])["messages"])
        report["sessions"] = len(archive.inbox(source.account))
        report["unread_sessions"] = len(archive.inbox(source.account, True))

        class ControlledFailure:
            account = source.account

            def scan(self, cursors=None):
                raise OSError("controlled_source_failure_no_source_modification")

        try:
            runtime.sync_once(archive, ControlledFailure())
        except OSError:
            report["fault_doctor"] = archive.doctor()
        report["recovery_sync"] = runtime.sync_once(archive, source)
        report["recovered_doctor"] = archive.doctor(max_age=120)
    after = fingerprints(source)
    report["source_files_sampled"] = len(before)
    report["source_files_unchanged"] = before == after
    runtime.write_json(runtime.DATA / "local-acceptance.json", report)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if report["fault_doctor"]["ok"] or not report["recovered_doctor"]["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
