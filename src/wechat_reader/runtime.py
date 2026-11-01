"""Runtime files stay in this checkout; source paths are never write targets."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"


def output_path(path):
    p = Path(path).resolve()
    if not p.is_relative_to(ROOT) or p.is_relative_to(ROOT / ".git"):
        raise ValueError("output_must_be_inside_project")
    # The runtime must not replace code, documentation or tracked files.
    if not p.is_relative_to(DATA):
        raise ValueError("runtime_output_must_be_inside_data_directory")
    return p


def write_json(path, value):
    path = output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def config():
    path = DATA / "config.json"
    if not path.exists():
        raise ValueError("not_configured_run_configure")
    return json.loads(path.read_text(encoding="utf-8"))


@contextlib.contextmanager
def lock(name):
    DATA.mkdir(exist_ok=True)
    with (DATA / (name + ".lock")).open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another_own_process_is_running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def sync_once(archive, source, *, full=False):
    totals = dict(inserted=0, updated=0, unchanged=0, batches=0)
    try:
        with lock("sync"):
            for batch in source.scan(cursors=None if full else archive.cursors(source.account)):
                r = archive.ingest(batch)
                for key in r:
                    totals[key] += r[key]
                totals["batches"] += 1
            archive.ingest(dict(account=source.account, source="__sync__", cursor=str(time.time())))
            # Reconciliation after every pass also repairs a crash between the
            # archive commit and the independent voice-queue registration.
            try:
                from .voice import VoiceQueue
                queue = VoiceQueue(archive.path.with_name(archive.path.stem + ".voices.db"))
                totals["voices_enqueued"] = queue.ensure_many(archive.voice_ids(source.account))
                archive.ingest(dict(account=source.account, source="__voice_queue__", cursor=str(time.time())))
            except Exception as exc:
                archive.failure(source.account, "__voice_queue__", type(exc).__name__)
                totals["voice_queue_error"] = type(exc).__name__
    except Exception as exc:
        archive.failure(source.account, "__sync__", type(exc).__name__)
        raise
    return totals


def daemon_state():
    path = DATA / "daemon.json"
    if not path.exists():
        return {"running": False}
    state = json.loads(path.read_text(encoding="utf-8"))
    pulse = DATA / "daemon.heartbeat"
    last_seen = pulse.stat().st_mtime if pulse.exists() else state.get("heartbeat", 0)
    state["running"] = bool(state.get("running") and time.time()-last_seen < 15)
    return state


def start_daemon(db_path, interval):
    if daemon_state().get("running"):
        return {"started": False, "detail": "already_running"}
    DATA.mkdir(exist_ok=True)
    stop = DATA / "daemon.stop"
    if stop.exists():
        stop.unlink()
    log = (DATA / "daemon.log").open("ab")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with log:
        started = time.time()
        proc = subprocess.Popen([sys.executable, "-m", "wechat_reader", "--db", str(db_path), "daemon", "run", "--interval", str(interval)], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
    # Windows venv launchers may delegate to a second PID; the daemon's own
    # fresh lock-protected state is authoritative, rather than the launcher PID.
    for _ in range(100):
        state = daemon_state()
        if state.get("running") and state.get("heartbeat", 0) >= started:
            return {"started": True, "pid": state["pid"]}
        time.sleep(.1)
    raise RuntimeError("daemon_start_timeout")


def run_daemon(db_path, interval):
    from .archive import Archive
    from .source import Source
    cfg = config()
    with lock("daemon"), Archive(db_path) as archive:
        state = dict(running=True, pid=os.getpid(), interval=interval, heartbeat=time.time())
        write_json(DATA / "daemon.json", state)
        stop = DATA / "daemon.stop"
        finished = threading.Event()

        def heartbeat():
            # Separate heartbeat file avoids racing the main state-file writer.
            while not finished.wait(2):
                (DATA / "daemon.heartbeat").touch()

        (DATA / "daemon.heartbeat").touch()
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            while not stop.exists():
                state.update(heartbeat=time.time(), syncing=True)
                write_json(DATA / "daemon.json", state)
                try:
                    cfg = config()
                    source = Source(Path(cfg["source"]), Path(cfg["key_file"]) if cfg.get("key_file") else None)
                    state["result"] = sync_once(archive, source)
                    state["error"] = None
                except Exception as exc:
                    state["error"] = type(exc).__name__
                    # Constructor failures must be visible in the archive as well.
                    archive.failure(cfg.get("account", "configured"), "__sync__", type(exc).__name__)
                state.update(heartbeat=time.time(), syncing=False)
                write_json(DATA / "daemon.json", state)
                deadline = time.monotonic()+interval
                while not stop.exists() and time.monotonic() < deadline:
                    time.sleep(.2)
        finally:
            finished.set()
            thread.join(timeout=3)
            state.update(running=False, heartbeat=time.time())
            write_json(DATA / "daemon.json", state)
            if stop.exists():
                stop.unlink()
