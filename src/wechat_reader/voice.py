"""Independent durable transcription queue; text ingestion never waits on a model."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Callable, Iterable, Iterator


class ModelUnavailable(RuntimeError):
    pass


class AudioUnsupported(RuntimeError):
    pass


class FasterWhisperTranscriber:
    """Offline model adapter. An explicit local model directory is required."""

    def __init__(self, model_dir: str | Path | None, *, cache_dir: str | Path, language: str | None = "zh"):
        self.model_dir = Path(model_dir).resolve() if model_dir else None
        self.cache_dir = Path(cache_dir).resolve()
        self.language = language
        self._model = None

    def __call__(self, audio_path: Path) -> str:
        if self.model_dir is None or not (self.model_dir / "model.bin").is_file():
            raise ModelUnavailable("local_model_missing")
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ModelUnavailable("faster_whisper_unavailable") from exc
        if self._model is None:
            self._model = WhisperModel(str(self.model_dir), device="cpu", compute_type="int8", local_files_only=True)
        with audio_path.open("rb") as stream:
            header = stream.read(16)
        if b"#!SILK_V3" in header:
            try:
                import pilk
            except ImportError as exc:
                raise AudioUnsupported("silk_decoder_unavailable") from exc
            # Decode only into the project's independent cache; never beside source audio.
            import hashlib
            import wave
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            stem = hashlib.sha256(audio_path.read_bytes()).hexdigest()
            token = uuid.uuid4().hex
            pcm_path = self.cache_dir / (stem + "." + token + ".pcm")
            wav_path = self.cache_dir / (stem + "." + token + ".wav")
            try:
                pilk.decode(str(audio_path), str(pcm_path), pcm_rate=24000)
                with wave.open(str(wav_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(pcm_path.read_bytes())
                segments, _ = self._model.transcribe(str(wav_path), language=self.language)
                return "".join(part.text for part in segments).strip()
            finally:
                pcm_path.unlink(missing_ok=True)
                wav_path.unlink(missing_ok=True)
        segments, _ = self._model.transcribe(str(audio_path), language=self.language)
        return "".join(part.text for part in segments).strip()


class VoiceQueue:
    """Jobs have persistent leases, so a crashed worker can be replaced safely."""

    def __init__(self, path: str | Path, *, lease_seconds: float = 900, clock: Callable[[], float] = time.time):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds_must_be_positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.lease_seconds = clock, lease_seconds
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS voice_jobs (message_id TEXT PRIMARY KEY, media_path TEXT, state TEXT NOT NULL, transcript TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, lease_until REAL, owner TEXT)")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def enqueue(self, message_id: str | int, media_path: str | Path | None) -> dict:
        identity = str(message_id)
        path = str(Path(media_path).resolve()) if media_path is not None else None
        with self._db() as db:
            db.execute("INSERT INTO voice_jobs(message_id,media_path,state,updated_at) VALUES(?,?,'pending',?) ON CONFLICT(message_id) DO UPDATE SET media_path=excluded.media_path,state='pending',transcript='',detail='',lease_until=NULL,owner=NULL,updated_at=excluded.updated_at WHERE voice_jobs.media_path IS NOT excluded.media_path AND voice_jobs.state!='running'", (identity, path, self.clock()))
        return self.get(identity)

    def ensure_many(self, message_ids: Iterable[str | int]) -> int:
        """Reconcile archive ids atomically without touching any existing job."""
        now = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                before = db.total_changes
                db.executemany(
                    "INSERT OR IGNORE INTO voice_jobs(message_id,media_path,state,updated_at) VALUES(?,NULL,'pending',?)",
                    ((str(identity), now) for identity in message_ids),
                )
                inserted = db.total_changes - before
                db.execute("COMMIT")
                return inserted
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def get(self, message_id: str | int) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM voice_jobs WHERE message_id=?", (str(message_id),)).fetchone()
            return dict(row) if row is not None else None

    def status(self) -> dict[str, int]:
        with self._db() as db:
            return {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM voice_jobs GROUP BY state")}

    def retry(self, message_id: str | int | None = None) -> int:
        with self._db() as db:
            sql = "UPDATE voice_jobs SET state='pending',detail='',lease_until=NULL,owner=NULL,updated_at=? WHERE state IN ('failed','missing','model_unavailable','unsupported')"
            args: list = [self.clock()]
            if message_id is not None:
                sql += " AND message_id=?"
                args.append(str(message_id))
            return db.execute(sql, args).rowcount

    def run_once(self, transcriber: Callable[[Path], str] | None = None, *,
                 media_loader: Callable[[str], Path | None] | None = None,
                 message_id: str | int | None = None) -> dict | None:
        now, owner = self.clock(), uuid.uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE voice_jobs SET state='pending',owner=NULL,lease_until=NULL,detail='previous_worker_lease_expired' WHERE state='running' AND lease_until<=?", (now,))
                sql = "SELECT * FROM voice_jobs WHERE state='pending'"
                parameters = []
                if message_id is not None:
                    sql += " AND message_id=?"
                    parameters.append(str(message_id))
                row = db.execute(sql + " ORDER BY updated_at,rowid LIMIT 1", parameters).fetchone()
                if row is None:
                    db.execute("COMMIT")
                    return None
                message_id, path = row["message_id"], row["media_path"]
                db.execute("UPDATE voice_jobs SET state='running',owner=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE message_id=?", (owner, now + self.lease_seconds, now, message_id))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        state, transcript, detail = "done", "", ""
        if media_loader is not None and (path is None or not Path(path).is_file()):
            try:
                loaded = media_loader(message_id)
                if loaded is not None:
                    path = str(Path(loaded).resolve())
                    with self._db() as db:
                        updated = db.execute(
                            "UPDATE voice_jobs SET media_path=?,updated_at=? WHERE message_id=? AND owner=? AND state='running'",
                            (path, self.clock(), message_id, owner),
                        ).rowcount
                    if not updated:
                        return self.get(message_id)
            except Exception as exc:
                state, detail = "failed", "media_load_failed:" + type(exc).__name__
        try:
            if state == "failed":
                pass
            elif path is None or not Path(path).is_file():
                state, detail = "missing", "local_voice_not_found"
            elif transcriber is None:
                state, detail = "model_unavailable", "transcriber_not_configured"
            else:
                transcript = transcriber(Path(path))
                if not isinstance(transcript, str):
                    raise TypeError("transcriber_must_return_text")
        except ModelUnavailable as exc:
            state, detail = "model_unavailable", str(exc)
        except AudioUnsupported as exc:
            state, detail = "unsupported", str(exc)
        except Exception as exc:
            # No private paths, audio contents or model diagnostics in status errors.
            state, detail = "failed", "transcription_failed:" + type(exc).__name__
        with self._db() as db:
            db.execute("UPDATE voice_jobs SET state=?,transcript=?,detail=?,updated_at=?,lease_until=NULL,owner=NULL WHERE message_id=? AND owner=?", (state, transcript, detail, self.clock(), message_id, owner))
        return self.get(message_id)
