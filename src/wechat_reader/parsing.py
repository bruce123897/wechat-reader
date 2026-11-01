"""Normalize WeChat message content without executing or fetching anything."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

_ZSTD = b"\x28\xb5\x2f\xfd"
_HEX = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_TYPES = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji", 48: "location", 49: "link", 50: "call", 10000: "system", 10002: "system"}


def _body(value: str | bytes | None) -> tuple[str, str | None]:
    if value is None:
        return "", None
    if isinstance(value, str):
        return value, None
    data = bytes(value)
    if data.startswith(_ZSTD):
        try:
            import zstandard
            data = zstandard.ZstdDecompressor().decompress(data, max_output_size=16 * 1024 * 1024)
        except ImportError:
            return "[压缩正文不可用]", "zstandard_unavailable"
        except Exception:
            return "[压缩正文损坏]", "zstandard_invalid"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "invalid_utf8"


def parse_message(content: str | bytes | None, local_type: int, *, chat: str = "", sender: str = "", is_me: bool = False, packed: bytes | str | None = None) -> dict[str, Any]:
    """Keep complete text and explicit degraded states; never invent recalled text."""
    raw, problem = _body(content)
    numeric = int(local_type or 0)
    primary, subtype = numeric & 0xffffffff, numeric >> 32
    extra: dict[str, Any] = {"source_type": numeric}
    if problem:
        extra["parse_error"] = problem
    text = raw
    if chat.endswith("@chatroom") and not is_me:
        # A group sender prefix is metadata only when it looks like an account id.
        prefix, sep, tail = raw.partition(":\n")
        if sep and (prefix == sender or prefix.startswith("wxid_") or "@" in prefix):
            extra["sender"] = prefix
            text = tail
    kind = _TYPES.get(primary, "unknown")
    if kind == "unknown":
        extra["unsupported_type"] = True
    if packed:
        binary = packed.encode("utf-8") if isinstance(packed, str) else bytes(packed)
        hashes = list(dict.fromkeys(x.decode("ascii").lower() for x in _HEX.findall(binary)))
        if hashes:
            extra["packed_hashes"] = hashes
    result = {"kind": kind, "text": text, "extra": extra, "recalled": False}
    if not text.lstrip().startswith("<"):
        return result
    # DTD/entity declarations have no place in a chat payload.
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        extra["parse_error"] = "xml_declaration_rejected"
        return result
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        extra["parse_error"] = "invalid_xml"
        return result

    def value(path: str, default: str = "") -> str:
        found = root.find(path)
        return "".join(found.itertext()) if found is not None else default

    def attrs(element: ET.Element | None, names: tuple[str, ...]) -> None:
        if element is not None:
            for name in names:
                item = element.get(name)
                if item is not None:
                    extra[name] = item

    revoke = root if root.tag == "sysmsg" and root.get("type") == "revokemsg" else None
    if revoke is not None:
        result.update(kind="recall", recalled=True, text=value(".//replacemsg", "[消息已撤回，原文未采集]"))
        extra["target_server_id"] = value(".//newmsgid") or value(".//msgid")
        extra["target_chat"] = value(".//session", chat)
        return result
    if primary == 3:
        attrs(root.find(".//img"), ("md5", "hdmd5", "length", "cdnthumbmd5"))
        result["text"] = "[图片]"
    elif primary == 34:
        attrs(root.find(".//voicemsg"), ("voiceformat", "voicelength", "length", "bufid", "clientmsgid", "md5"))
        result["text"] = "[语音]"
    elif primary == 43:
        attrs(root.find(".//videomsg"), ("md5", "length", "playlength", "cdnthumbmd5"))
        result["text"] = "[视频]"
    elif primary == 47:
        attrs(root.find(".//emoji"), ("md5", "type"))
        result["text"] = "[表情]"
    elif primary == 48:
        attrs(root.find(".//location"), ("x", "y", "label", "poiname"))
        result["text"] = extra.get("label") or extra.get("poiname") or "[位置]"
    elif primary == 49:
        app = root if root.tag == "appmsg" else root.find(".//appmsg")
        if app is None:
            extra["parse_error"] = "appmsg_missing"
            return result
        app_type = app.findtext("type", str(subtype or 0))
        extra["app_type"] = app_type
        title = app.findtext("title", "")
        result["text"] = title or "[应用消息]"
        if app_type == "57":
            result["kind"] = "quote"
            ref = app.find("refermsg")
            if ref is not None:
                extra["reference"] = {name: ref.findtext(name, "") for name in ("svrid", "fromusr", "chatusr", "displayname", "content", "type")}
        elif app_type == "6":
            result["kind"] = "file"
            extra["name"] = title
            attach = app.find("appattach")
            if attach is not None:
                for name in ("md5", "fileext", "totallen", "attachid"):
                    val = attach.findtext(name)
                    if val is not None:
                        extra[name] = val
        elif app_type == "19":
            result["kind"] = "record"
        elif app_type in ("33", "36"):
            result["kind"] = "miniapp"
        elif app_type not in ("5", "0"):
            extra["unsupported_subtype"] = True
        md5 = app.findtext("md5")
        if md5:
            extra["md5"] = md5
    elif kind == "system":
        result["text"] = value(".//plain") or "".join(root.itertext()) or "[系统事件]"
    return result
