"""Making a binary column readable.

The CC stores Java-serialised objects in BLOB columns — quartz's JOB_DATA is a
`JobDataMap`, and the part an engineer actually wants is usually a JSON string
buried inside it. Opening the download in a text editor shows the serialisation
framing around it, which is why it looks like line noise with words in it.

What this does NOT do is pretend to be a Java deserialiser. Reconstructing the
object graph would need the CC's own classes to be meaningful, and a wrong
reconstruction that looks plausible is worse than no reconstruction. Instead it
reports what the bytes ARE, pulls out the readable strings, and — when one of
those strings parses as JSON — hands that over as the payload. Everything it
returns can be pointed at a byte range in the original, so nothing is invented.
"""

from __future__ import annotations

import json
import re
import zlib

# Format signatures, longest-first where they overlap.
_MAGIC = [
    (b"\xac\xed", "java-serialized", "Java serialised object"),
    (b"\x1f\x8b", "gzip", "gzip-compressed data"),
    (b"PK\x03\x04", "zip", "ZIP archive"),
    (b"\x89PNG\r\n\x1a\n", "png", "PNG image"),
    (b"\xff\xd8\xff", "jpeg", "JPEG image"),
    (b"%PDF-", "pdf", "PDF document"),
    (b"SQLite format 3\x00", "sqlite", "SQLite database"),
]

# Runs of printable ASCII. 4 is the usual `strings` threshold: shorter runs are
# mostly coincidence in binary framing.
_RUNS = re.compile(rb"[ -~\t]{4,}")

# Java writes a string as TC_STRING (0x74) + uint16 length + modified UTF-8.
_TC_STRING = 0x74
_TC_LONGSTRING = 0x7C


def _java_strings(data: bytes) -> list[str]:
    """Strings recovered by following Java's own length prefixes.

    More precise than scanning for printable runs: the length prefix says where
    a string ends, so values containing punctuation or newlines come back whole
    instead of being chopped at the first non-printable byte.
    """
    out: list[str] = []
    i, n = 0, len(data)
    while i < n:
        tag = data[i]
        if tag == _TC_STRING and i + 3 <= n:
            length = int.from_bytes(data[i + 1:i + 3], "big")
            start, end = i + 3, i + 3 + length
            if length and end <= n:
                try:
                    out.append(data[start:end].decode("utf-8"))
                    i = end
                    continue
                except UnicodeDecodeError:
                    pass
        elif tag == _TC_LONGSTRING and i + 9 <= n:
            length = int.from_bytes(data[i + 1:i + 9], "big")
            start, end = i + 9, i + 9 + length
            if 0 < length <= n and end <= n:
                try:
                    out.append(data[start:end].decode("utf-8"))
                    i = end
                    continue
                except UnicodeDecodeError:
                    pass
        i += 1
    return out


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    ok = sum(1 for ch in text if ch.isprintable() or ch in "\t\n\r")
    return ok / len(text)


def _printable_runs(data: bytes) -> list[str]:
    return [m.group().decode("ascii", "replace") for m in _RUNS.finditer(data)]


def _find_json(candidates: list[str]) -> tuple[object | None, str]:
    """The first candidate that parses as a JSON object or array.

    Only whole strings are tried. Hunting for a `{` inside a longer string and
    guessing where it ends is how you end up displaying half a document as if
    it were the whole thing.
    """
    for s in candidates:
        t = s.strip()
        if len(t) < 2 or t[0] not in "{[":
            continue
        try:
            return json.loads(t), t
        except ValueError:
            continue
    return None, ""


def describe(data: bytes, max_text: int = 200_000) -> dict:
    """What this blob is, and whatever text can be read out of it."""
    kind, label = "binary", "unrecognised binary data"
    for magic, k, human in _MAGIC:
        if data.startswith(magic):
            kind, label = k, human
            break

    # gzip is worth unwrapping: the interesting content is one inflate away, and
    # leaving it packed would report "compressed data" and stop being useful.
    inner = b""
    if kind == "gzip":
        try:
            inner = zlib.decompress(data, 16 + zlib.MAX_WBITS)
        except zlib.error:
            inner = b""
    body = inner or data

    # Plain text that happens to live in a BLOB column — common enough, and it
    # should not be presented as if it needed decoding.
    if kind == "binary":
        try:
            text = body.decode("utf-8")
            # Decoding cleanly is not enough to call it text: a run of control
            # bytes is valid UTF-8 too, and labelling that "UTF-8 text" would
            # show an engineer a pane of invisible characters and call it the
            # content. Require it to be overwhelmingly printable.
            if text and _printable_ratio(text) >= 0.95:
                parsed, raw = _find_json([text])
                return {"kind": "text", "label": "UTF-8 text",
                        "size": len(data), "strings": [],
                        "json": parsed, "json_text": raw,
                        "text": text[:max_text],
                        "truncated": len(text) > max_text}
        except UnicodeDecodeError:
            pass

    strings = _java_strings(body) if kind == "java-serialized" else []
    if not strings:
        strings = _printable_runs(body)

    parsed, raw = _find_json(strings)

    return {
        "kind": kind,
        "label": label + (" (gzip-wrapped)" if inner else ""),
        "size": len(data),
        # Deduplicated, order preserved: Java repeats class and field names
        # throughout the stream and the repeats carry no information.
        "strings": list(dict.fromkeys(s for s in strings if s.strip()))[:500],
        "json": parsed,
        "json_text": raw,
        "text": "",
        "truncated": False,
    }
