"""OFFLINE CS2 inspect-link decoder -- turns an in-game inspect link into
{def_index, paint_index, paint_seed, float_value, rarity, sticker_ids} with ZERO network calls.

Why this module exists (2026-07-22): pattern_scanner.py used to resolve every Steam listing's
float/seed through CSFloat's hosted inspect API. That API is GONE -- api.csfloat.com no longer
resolves at all (NXDOMAIN, verified against 8.8.8.8, not a local DNS quirk) and the
csfloat/inspect repo was archived on 2026-03-25 with this note:

    "As of March 2026, all CS2 inspect links self-encode the item details, please use libraries
     such as @csfloat/cs-inspect-serializer."

That is the whole trick the fast browser extensions use. CS2 Float Scanner advertises "instant
float display -- no delays or queues" and "full privacy -- no external requests" precisely
because a modern inspect link CARRIES the item data in its own payload; nothing needs to ask a
game-coordinator bot anymore. Decoding is pure local CPU, microseconds per link, no rate limit,
no API key, no third party. Doing this per listing is what turns the scanner from
"one network round-trip + 1.5s of politeness sleep per item" into "free".

Link variants handled (see the `cs2-masked-inspect` / `cs2inspect` prior art):
  - MASKED, native CS2  -- `...+csgo_econ_action_preview <hex>` where hex[0] is a non-zero key
    byte and every following byte is XOR'd with it.
  - MASKED, tool-generated -- same, but key byte 0x00 means no XOR.
  - HYBRID -- `S<steamid>A<assetid>D<hex payload>`: the D-part is a masked hex payload.
  - CLASSIC/LEGACY -- `M<listingid>A<assetid>D<decimal d-token>`: this one carries NO item data,
    only opaque handles the game coordinator dereferences. It is NOT decodable offline and never
    will be. decode_inspect_link() returns None for it -- honestly, so the caller can fall back
    to a real inspect service instead of silently reporting a miss (see pattern_scanner's
    _resolve_item). Classic vs hybrid is decided by ACTUALLY DECODING, not by guessing from the
    shape: a payload only counts as masked if it parses into a plausible item (see _plausible).

The payload is a `CEconItemPreviewDataBlock` protobuf. Only the handful of fields the pattern
scanner matches on are pulled out; everything else is skipped generically, so an unknown/new
field never breaks the parse. Deliberately stdlib-only (struct/binascii/re) to match this
project's "core is zero-dependency" rule in requirements.txt.
"""
import binascii
import re
import struct

# CEconItemPreviewDataBlock field numbers (Valve's econ.proto).
_F_DEFINDEX = 3
_F_PAINTINDEX = 4
_F_RARITY = 5
_F_QUALITY = 6
_F_PAINTWEAR = 7   # uint32 holding the IEEE-754 float32 BITS of the wear, not a scaled int
_F_PAINTSEED = 8
_F_STICKERS = 12   # repeated Sticker
_F_KEYCHAINS = 20  # repeated Sticker (same sub-message shape)
_F_STICKER_ID = 2  # Sticker.sticker_id

# NOTE the alternation: a character class like [\s%20]* would also match the digits '2' and '0'
# and would happily eat the leading "00" of a tool-generated (key byte 0x00) payload.
_PREVIEW_SPLIT = re.compile(r"csgo_econ_action_preview(?:%20|\s|\+)*", re.IGNORECASE)
_LEAD_SEP = re.compile(r"^(?:%20|\s|\+)+")
_HYBRID_RE = re.compile(r"^[SM]\d+A\d+D([0-9A-Fa-f]+)$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
_CLASSIC_RE = re.compile(r"^[SM]\d+A\d+D\d+$")

MAX_PAYLOAD_HEX = 4096  # same sanity ceiling the Rust/py implementations use


class _Reader:
    """Minimal protobuf wire reader. Raises ValueError on malformed input."""

    def __init__(self, buf: bytes):
        self.buf = buf
        self.i = 0

    def eof(self) -> bool:
        return self.i >= len(self.buf)

    def varint(self) -> int:
        result = 0
        shift = 0
        while True:
            if self.i >= len(self.buf):
                raise ValueError("truncated varint")
            if shift > 63:
                raise ValueError("varint too long")
            b = self.buf[self.i]
            self.i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    def take(self, n: int) -> bytes:
        if n < 0 or self.i + n > len(self.buf):
            raise ValueError("truncated field")
        out = self.buf[self.i:self.i + n]
        self.i += n
        return out


def _parse_fields(buf: bytes) -> dict:
    """field_number -> list of decoded values (int for varint/fixed, bytes for length-delimited).

    Parses greedily and STOPS at the first malformed field rather than failing outright: masked
    payloads carry a short trailer (checksum bytes) after the message, and a partial parse that
    already yielded defindex/paintindex/paintseed is strictly more useful than an exception.
    """
    r = _Reader(buf)
    out: dict = {}
    while not r.eof():
        start = r.i
        try:
            tag = r.varint()
            field, wire = tag >> 3, tag & 0x07
            if field == 0:
                raise ValueError("field 0")
            if wire == 0:
                val = r.varint()
            elif wire == 1:
                val = int.from_bytes(r.take(8), "little")
            elif wire == 2:
                val = r.take(r.varint())
            elif wire == 5:
                val = int.from_bytes(r.take(4), "little")
            else:
                raise ValueError(f"bad wire type {wire}")
        except ValueError:
            r.i = start
            break
        out.setdefault(field, []).append(val)
    return out


def _first(fields: dict, num: int):
    vals = fields.get(num)
    return vals[0] if vals else None


def _sticker_ids(fields: dict, num: int) -> list:
    ids = []
    for raw in fields.get(num) or []:
        if not isinstance(raw, bytes):
            continue
        try:
            sid = _first(_parse_fields(raw), _F_STICKER_ID)
        except ValueError:
            continue
        if isinstance(sid, int):
            ids.append(sid)
    return ids


def _wear_from_bits(bits) -> float | None:
    """paintwear is a uint32 carrying float32 bits -- reinterpret, don't divide."""
    if not isinstance(bits, int) or not 0 <= bits <= 0xFFFFFFFF:
        return None
    try:
        return struct.unpack("<f", struct.pack("<I", bits))[0]
    except (struct.error, OverflowError):
        return None


def _plausible(item: dict) -> bool:
    """Is this a real item, or did we just parse random bytes into coincidental fields?

    This is what separates a masked payload from a classic link's decimal d-token, so it has to
    be strict enough that garbage never sneaks through as a fake "match".
    """
    if not isinstance(item.get("def_index"), int) or not 0 < item["def_index"] < 100000:
        return False
    if item.get("paint_index") is None or not 0 <= item["paint_index"] < 100000:
        return False
    fv = item.get("float_value")
    if fv is None or not 0.0 <= fv <= 1.0:
        return False
    seed = item.get("paint_seed")
    if seed is not None and not 0 <= seed <= 100000:
        return False
    return True


def decode_hex(payload_hex: str) -> dict | None:
    """Decode a masked inspect payload (hex string). None if it isn't a decodable item."""
    payload_hex = payload_hex.strip()
    if not payload_hex or len(payload_hex) > MAX_PAYLOAD_HEX or len(payload_hex) % 2:
        return None
    if not _HEX_RE.match(payload_hex):
        return None
    try:
        data = binascii.unhexlify(payload_hex)
    except (binascii.Error, ValueError):
        return None
    if len(data) < 4:
        return None
    key, body = data[0], data[1:]
    if key:
        body = bytes(b ^ key for b in body)
    try:
        fields = _parse_fields(body)
    except ValueError:
        return None
    # Protobuf omits any field whose value equals the default, so an ABSENT paintindex/paintseed
    # means 0, not "unknown". Verified live: a real listing decoded with paint_index=44 but no
    # paintseed field, i.e. seed 0 -- reporting None there would make seed-0 items unmatchable
    # and show a blank seed in the UI. def_index/paintwear are left strict: a missing one means
    # the payload is not a real item, which _plausible() relies on to reject garbage.
    item = {
        "def_index": _first(fields, _F_DEFINDEX),
        "paint_index": _first(fields, _F_PAINTINDEX) or 0,
        "paint_seed": _first(fields, _F_PAINTSEED) or 0,
        "float_value": _wear_from_bits(_first(fields, _F_PAINTWEAR)),
        "rarity": _first(fields, _F_RARITY),
        "quality": _first(fields, _F_QUALITY),
        "sticker_ids": _sticker_ids(fields, _F_STICKERS),
        "keychain_ids": _sticker_ids(fields, _F_KEYCHAINS),
    }
    return item if _plausible(item) else None


def decode_inspect_link(link: str) -> dict | None:
    """Decode any CS2 inspect link OFFLINE.

    Returns the item dict, or None when the link is a classic M/A/D link (no embedded data --
    the caller must fall back to an inspect service) or simply isn't decodable.
    """
    if not link or not isinstance(link, str):
        return None
    tail = _LEAD_SEP.sub("", _PREVIEW_SPLIT.split(link)[-1].strip()).strip()
    if not tail:
        return None
    hybrid = _HYBRID_RE.match(tail)
    if hybrid:
        # S…A…D<hex> -- masked payload riding in the D slot. A CLASSIC link also matches this
        # shape when its decimal d-token happens to be all 0-9, so decode_hex's _plausible()
        # check is what actually decides; a classic token will not parse into a real item.
        return decode_hex(hybrid.group(1))
    return decode_hex(tail)


def is_classic_link(link: str) -> bool:
    """True for legacy M/A/D links that carry no item data (need a real inspect service)."""
    if not link:
        return False
    tail = _LEAD_SEP.sub("", _PREVIEW_SPLIT.split(link)[-1].strip()).strip()
    return bool(_CLASSIC_RE.match(tail)) and decode_inspect_link(link) is None
