"""Locate local attachments and decode supported image containers into own cache."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import struct
from typing import Any


def _format(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"wxgf"):
        return "wxgf"
    return None


def _sniff(path: Path) -> tuple[str, str]:
    """Identify content, never infer a video from its filename extension."""
    with path.open("rb") as stream:
        header = stream.read(64)
    image = _format(header)
    if image:
        return "image", image
    if header[:6] in (b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"):
        return "image", "encrypted_dat"
    if len(header) >= 16 and header[4:8] == b"ftyp":
        box_size = int.from_bytes(header[:4], "big")
        if 16 <= box_size <= path.stat().st_size:
            brand = header[8:12]
            if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis"):
                return "image", "avif" if brand in (b"avif", b"avis") else "heif"
            if brand in (b"M4A ", b"M4B ", b"M4P "):
                return "audio", "mp4_audio"
            if brand in (b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"qt  ", b"dash") or brand.startswith(b"3gp"):
                return "video", "mov" if brand == b"qt  " else "mp4"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return "video", "avi"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video", "matroska"
    if header.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video", "mpeg"
    if path.suffix.lower() == ".dat" and header:
        for signature in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF"):
            mask = header[0] ^ signature[0]
            prefix = bytes(byte ^ mask for byte in header)
            image = _format(prefix)
            if prefix.startswith(signature) and image:
                return "image", "encrypted_dat"
    return "unknown", "unknown"


def decode_image(data: bytes, *, image_key: bytes | str | None = None, image_xor: int | str | None = None) -> tuple[bytes, str]:
    """Decode format-defined AES/clear/XOR sections; reject malformed boundaries."""
    plain_format = _format(data)
    if plain_format:
        return data, plain_format
    if len(data) < 16:
        raise ValueError("image_container_truncated")
    if data[:6] in (b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"):
        encrypted_length, xor_length = struct.unpack_from("<II", data, 6)
        encrypted_blocks = ((encrypted_length // 16) + 1) * 16
        payload = memoryview(data)[15:]
        if encrypted_blocks > len(payload) or xor_length > len(payload) - encrypted_blocks:
            raise ValueError("image_section_out_of_bounds")
        key = b"cfcd208495d565ef" if data[3] == ord("1") else image_key
        if isinstance(key, str):
            key = key.encode("utf-8")[:16]
        if key is None:
            raise LookupError("image_key_unavailable")
        if len(key) not in (16, 24, 32):
            raise ValueError("image_key_invalid_length")
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as exc:
            raise LookupError("cryptography_unavailable") from exc
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        decrypted = decryptor.update(payload[:encrypted_blocks]) + decryptor.finalize()
        padding = encrypted_blocks - encrypted_length
        if decrypted[encrypted_length:] != bytes([padding]) * padding:
            raise ValueError("image_key_or_padding_invalid")
        head = decrypted[:encrypted_length]
        split = len(payload) - xor_length
        tail = bytes(payload[split:])
        if xor_length:
            if image_xor in (None, ""):
                # Only infer a footer mask when two independent JPEG footer bytes agree.
                if head.startswith(b"\xff\xd8\xff") and xor_length >= 2 and (tail[-2] ^ 0xff) == (tail[-1] ^ 0xd9):
                    mask = tail[-2] ^ 0xff
                else:
                    raise LookupError("image_xor_unavailable")
            else:
                mask = int(image_xor)
            if not 0 <= mask <= 255:
                raise ValueError("image_xor_invalid")
            tail = tail.translate(bytes(i ^ mask for i in range(256)))
        output = head + bytes(payload[encrypted_blocks:split]) + tail
        found = _format(output)
        if not found:
            raise ValueError("decoded_image_format_unknown")
        return output, found
    # The legacy format uses one XOR byte for the entire image. Require a full signature.
    for signature in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF"):
        mask = data[0] ^ signature[0]
        prefix = bytes(x ^ mask for x in data[:max(12, len(signature))])
        if prefix.startswith(signature):
            decoded = data.translate(bytes(i ^ mask for i in range(256)))
            found = _format(decoded)
            if found:
                return decoded, found
    raise LookupError("image_format_unsupported")


class MediaResolver:
    """A local-only resolver. Symlinks and explicit paths must remain in account root."""

    def __init__(self, account_root: str | Path, cache_dir: str | Path | None = None, *, image_key: bytes | str | None = None, image_xor: int | str | None = None):
        self.root = Path(account_root).resolve()
        self.cache = Path(cache_dir).resolve() if cache_dir is not None else None
        if self.cache is not None and self.cache.is_relative_to(self.root):
            raise ValueError("cache_must_be_outside_source")
        self.image_key, self.image_xor = image_key, image_xor
        self._files: list[Path] | None = None
        self._by_name: dict[str, list[Path]] = {}
        self._by_hash: dict[str, list[Path]] = {}

    def refresh(self) -> None:
        self._files = None
        self._by_name.clear()
        self._by_hash.clear()

    def _safe(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("media_path_outside_account")
        return resolved

    def _index(self) -> list[Path]:
        if self._files is None:
            self._files = []
            base = self.root / "msg"
            if base.is_dir():
                for candidate in base.rglob("*"):
                    if candidate.is_file():
                        try:
                            safe = self._safe(candidate)
                            self._files.append(safe)
                            self._by_name.setdefault(safe.name.casefold(), []).append(safe)
                            leading_hash = re.match(r"[0-9a-f]{32}", safe.name.lower())
                            if leading_hash:
                                self._by_hash.setdefault(leading_hash[0], []).append(safe)
                        except ValueError:
                            continue
        return self._files

    def resolve(self, message: dict[str, Any], *, decode: bool = True) -> dict[str, Any]:
        extra = message.get("extra") or {}
        if isinstance(extra, str):
            extra = json.loads(extra)
        explicit = extra.get("path") or extra.get("file_path")
        files: list[Path] = []
        evidence: dict[Path, set[str]] = {}
        if explicit:
            try:
                path = Path(str(explicit))
                candidate = self._safe(path if path.is_absolute() else self.root / path)
            except (ValueError, OSError):
                return {"state": "error", "path": None, "detail": "media_path_outside_account"}
            files = [candidate] if candidate.is_file() else []
            if files:
                evidence[candidate] = {"explicit"}
        else:
            hashes = {str(extra[k]).lower() for k in ("md5", "hdmd5", "cdnthumbmd5") if extra.get(k)}
            hashes.update(str(x).lower() for x in extra.get("packed_hashes", []))
            hashes = {x for x in hashes if re.fullmatch("[0-9a-f]{32}", x)}
            name = str(extra.get("name", ""))
            self._index()
            if hashes:
                for field in ("hdmd5", "md5", "packed_hashes", "cdnthumbmd5"):
                    values = extra.get(field, []) if field == "packed_hashes" else [extra.get(field)]
                    for value in values:
                        if not value:
                            continue
                        for candidate in self._by_hash.get(str(value).lower(), []):
                            evidence.setdefault(candidate, set()).add(field)
                            files.append(candidate)
            if not files and name and Path(name).name == name:
                files = self._by_name.get(name.casefold(), [])
                for candidate in files:
                    evidence.setdefault(candidate, set()).add("name")
                # A file attachment's XML MD5 is stronger evidence than its title.
                # Refuse same-named local files whose content belongs to another version.
                expected = str(extra.get("md5", "")).lower()
                if message.get("kind") == "file" and re.fullmatch("[0-9a-f]{32}", expected):
                    checked = []
                    for candidate in files:
                        try:
                            with candidate.open("rb") as stream:
                                if hashlib.file_digest(stream, "md5").hexdigest() == expected:
                                    checked.append(candidate)
                        except OSError:
                            continue
                    if files and not checked:
                        return {"state": "missing", "path": None, "detail": "local_file_hash_mismatch"}
                    files = checked
        if not files:
            return {"state": "missing", "path": None, "detail": "local_media_not_found"}
        files = list(dict.fromkeys(files))
        requested_kind = message.get("kind", "unknown")
        candidates = []
        for candidate in files:
            try:
                actual_kind, actual_format = _sniff(candidate)
            except OSError:
                continue
            fields = evidence.get(candidate, set())
            thumb = bool(re.search(r"_(?:t|thumb)\.", candidate.name, re.I)) or ("cdnthumbmd5" in fields and not fields.intersection({"md5", "hdmd5", "explicit", "name"}))
            # A video cover is an image even when it shares the video's hash or name.
            if requested_kind == "video" and actual_kind == "image":
                thumb = True
            role = "thumbnail" if thumb else "original"
            if requested_kind == "image" and role == "original" and not fields.intersection({"md5", "hdmd5", "explicit", "name"}):
                role = "unclassified"
            rank = min(({"hdmd5": 0, "md5": 1, "explicit": 0, "name": 1, "packed_hashes": 2, "cdnthumbmd5": 3}[field] for field in fields), default=4)
            candidates.append(dict(path=candidate, media_kind=actual_kind, format=actual_format, variant=role, rank=rank))
        if not candidates:
            return {"state": "missing", "path": None, "detail": "local_media_disappeared"}
        if requested_kind == "video":
            originals = [item for item in candidates if item["media_kind"] == "video" and item["variant"] == "original"]
            fallback = [item for item in candidates if item["media_kind"] == "image"]
            pool = originals or fallback
            if not pool:
                return {"state": "unsupported", "path": None, "detail": "video_format_unverified", "requested_kind": "video", "source_media_missing": True}
        elif requested_kind == "image":
            images = [item for item in candidates if item["media_kind"] == "image"]
            pool = [item for item in images if item["variant"] == "original"] or [item for item in images if item["variant"] == "unclassified"] or images
            if not pool:
                return {"state": "unsupported", "path": None, "detail": "image_format_unverified", "requested_kind": "image", "source_media_missing": True}
        else:
            pool = [item for item in candidates if item["variant"] == "original"] or candidates
        strongest = min(item["rank"] for item in pool)
        pool = [item for item in pool if item["rank"] == strongest]
        if len(pool) != 1:
            return {"state": "ambiguous", "path": None, "detail": "multiple_local_candidates", "candidates": len(pool)}
        selected = pool[0]
        chosen, variant = selected["path"], selected["variant"]
        try:
            size = chosen.stat().st_size
        except FileNotFoundError:
            return {"state": "missing", "path": None, "detail": "local_media_disappeared"}
        except OSError:
            return {"state": "error", "path": None, "detail": "local_media_stat_failed"}
        source_missing = variant == "thumbnail"
        actual_kind = selected["media_kind"]
        if actual_kind == "unknown" and requested_kind == "file":
            actual_kind = "file"
        result: dict[str, Any] = {"state": "available", "path": str(chosen), "detail": "source_media_missing" if source_missing else "local_file", "variant": variant, "size": size,
            "requested_kind": requested_kind, "media_kind": actual_kind, "format": selected["format"], "source_media_missing": source_missing}
        if chosen.suffix.lower() != ".dat" or not decode or actual_kind == "video":
            return result
        if self.cache is None:
            return {**result, "state": "unsupported", "detail": "decoded_cache_not_configured"}
        try:
            source = chosen.read_bytes()
            decoded, extension = decode_image(source, image_key=self.image_key, image_xor=self.image_xor)
            if extension == "wxgf":
                return {**result, "state": "unsupported", "detail": "wxgf_requires_converter"}
            self.cache.mkdir(parents=True, exist_ok=True)
            target = self.cache / (hashlib.sha256(source).hexdigest() + "." + extension)
            if not target.is_file():
                # Exclusive creation prevents readers from observing partial cache files.
                import os
                import uuid
                temporary = self.cache / (target.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(decoded)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            return {**result, "path": str(target), "detail": "source_media_missing" if source_missing else "decoded_local_image", "size": len(decoded), "format": extension, "media_kind": "image", "decoded": True}
        except LookupError as exc:
            return {**result, "state": "unsupported", "detail": str(exc)}
        except (ValueError, OSError):
            return {**result, "state": "error", "detail": "media_decode_failed"}

    def read(self, message: dict[str, Any], *, max_bytes: int = 64 * 1024 * 1024) -> tuple[dict[str, Any], bytes | None]:
        result = self.resolve(message)
        if result["state"] != "available":
            return result, None
        if result["size"] > max_bytes:
            return {**result, "state": "error", "detail": "media_exceeds_read_limit"}, None
        try:
            return result, Path(result["path"]).read_bytes()
        except OSError:
            return {**result, "state": "error", "detail": "media_read_failed"}, None
