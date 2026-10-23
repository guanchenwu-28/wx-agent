"""Conservative, offline ARM64 Mach-O adapter matching (standard library only).

This module never loads target code. A static match is NOT ABI or delivery proof.
PC-relative immediates are normalized, while object field offsets are retained.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import struct
import uuid
from pathlib import Path


DEFAULT_OFFSETS = {
    "service_getter": 0x42AF2F0, "executor_getter": 0x42B08D4,
    "context_factory": 0x42B0808, "context_mid": 0x1661598,
    "manager_from_mid": 0x2419234, "message_submit": 0x2E331D4,
    "result_destroy": 0x2E2EB58, "executor_schedule": 0x41842C8,
    "text_factory": 0x73EEC0, "image_factory": 0x43F108,
    "voice_factory": 0x40D8618,
}
DEFAULT_VTABLES = {"manager": 0x98C2D70, "text": 0x98E5298,
                   "image": 0x98E52E8, "voice": 0x98B4280}
DEFAULT_LAYOUT = {
    "recipient": 144, "text": 1480, "type": 228, "text_length": 368,
    "image_path": 240, "voice_payload": 1480, "voice_duration": 1504,
    "manager_adjust": -16, "manager_ready": 24, "service_manager_slot": 24,
    "service_login_slot": 32, "result_begin": 32, "result_end": 40,
}


class MatchError(ValueError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


class MachOImage:
    """Strict file-backed thin/fat ARM64 reader; offsets exposed relative to base."""

    def __init__(self, core_path):
        self.path = Path(core_path).expanduser().resolve()
        whole = self.path.read_bytes()
        self.core_sha256 = _sha(whole)
        self.slice_offset, self.slice_size = self._slice(whole)
        self.data = whole[self.slice_offset:self.slice_offset + self.slice_size]
        self.arm64_sha256 = _sha(self.data)
        if len(self.data) < 32:
            raise MatchError("truncated Mach-O header")
        hdr = self.unpack("<8I", 0)
        if hdr[0] != 0xFEEDFACF or hdr[1] != 0x100000C:
            raise MatchError("target is not little-endian ARM64 Mach-O")
        if (hdr[2] & 0xFFFFFF) != 0:
            raise MatchError("ARM64e or unknown CPU subtype requires a distinct profile")
        ncmds, sizeofcmds = hdr[4:6]
        if ncmds > 10000 or 32 + sizeofcmds > len(self.data):
            raise MatchError("invalid load command bounds")
        self.segments, self.sections = [], []
        starts_command = chained_command = None
        off = 32
        self.uuid = None
        for _ in range(ncmds):
            cmd, size = self.unpack("<II", off)
            if size < 8 or off + size > 32 + sizeofcmds:
                raise MatchError("invalid load command size")
            if cmd == 0x19:
                if size < 72:
                    raise MatchError("truncated segment command")
                s = self.unpack("<II16sQQQQIIII", off)
                name = s[2].split(b"\0")[0].decode("ascii")
                seg = dict(name=name, vmaddr=s[3], vmsize=s[4], fileoff=s[5], filesize=s[6], initprot=s[8])
                if s[5] + s[6] > len(self.data) or s[6] > s[4]:
                    raise MatchError("truncated segment data")
                if s[9] > 10000 or 72 + s[9] * 80 > size:
                    raise MatchError("truncated section commands")
                self.segments.append(seg)
                for j in range(s[9]):
                    q = self.unpack("<16s16sQQIIIIIIII", off + 72 + j * 80)
                    sec = dict(name=q[0].split(b"\0")[0].decode("ascii"), segment=name,
                               addr=q[2], size=q[3], offset=q[4], flags=q[8])
                    if not (s[3] <= q[2] <= q[2] + q[3] <= s[3] + s[4]):
                        raise MatchError("section outside segment")
                    self.sections.append(sec)
            elif cmd in (0x26, 0x80000034):
                if size < 16:
                    raise MatchError("truncated linkedit command")
                bounds = self.unpack("<II", off + 8)
                self.checked(*bounds)
                if cmd == 0x26:
                    starts_command = bounds
                else:
                    chained_command = bounds
            elif cmd == 0x1B:
                self.uuid = str(uuid.UUID(bytes=bytes(self.checked(off + 8, 16))))
            off += size
        if off != 32 + sizeofcmds:
            raise MatchError("load command total size mismatch")
        texts = [s for s in self.segments if s["name"] == "__TEXT"]
        if len(texts) != 1 or not starts_command:
            raise MatchError("exactly one __TEXT and LC_FUNCTION_STARTS required")
        self.imagebase = texts[0]["vmaddr"]
        self.module_size = texts[0]["vmsize"]
        mapped = [s for s in self.segments if s["vmsize"] and s["name"] != "__PAGEZERO"]
        self.image_size = max(s["vmaddr"] + s["vmsize"] for s in mapped) - self.imagebase
        self.function_starts = self._function_starts(*starts_command)
        self.function_set = set(self.function_starts)
        self._fingerprints = {}
        self._calls = None
        self.chained_pointers = self._chained_fixups(*chained_command) if chained_command else {}

    @staticmethod
    def _slice(data):
        if len(data) < 8:
            raise MatchError("truncated file")
        magic = data[:4]
        if magic == b"\xcf\xfa\xed\xfe":
            return 0, len(data)
        formats = {b"\xca\xfe\xba\xbe": (">", False), b"\xbe\xba\xfe\xca": ("<", False),
                   b"\xca\xfe\xba\xbf": (">", True), b"\xbf\xba\xfe\xca": ("<", True)}
        if magic not in formats:
            raise MatchError("not a supported Mach-O file")
        endian, wide = formats[magic]
        count = struct.unpack_from(endian + "I", data, 4)[0]
        stride = 32 if wide else 20
        if count > 64 or 8 + count * stride > len(data):
            raise MatchError("invalid universal binary table")
        found, ranges = [], []
        for i in range(count):
            row = struct.unpack_from(endian + ("IIQQII" if wide else "IIIII"), data, 8 + i * stride)
            cpu, subtype, off, size, align = row[:5]
            if size == 0 or off < 8 + count * stride or off + size > len(data) or align > 31 or off % (1 << align):
                raise MatchError("invalid universal slice bounds/alignment")
            if any(max(a, off) < min(b, off + size) for a, b in ranges):
                raise MatchError("overlapping universal slices")
            ranges.append((off, off + size))
            if cpu == 0x100000C and (subtype & 0xFFFFFF) == 0:
                found.append((off, size))
        if len(found) != 1:
            raise MatchError("exactly one ARM64 slice required")
        return found[0]

    def checked(self, off, size):
        if off < 0 or size < 0 or off + size > len(self.data):
            raise MatchError("file range outside ARM64 slice")
        return memoryview(self.data)[off:off + size]

    def unpack(self, fmt, off):
        self.checked(off, struct.calcsize(fmt))
        return struct.unpack_from(fmt, self.data, off)

    def file_offset(self, relative, size=1):
        addr = self.imagebase + relative
        for s in self.segments:
            if s["vmaddr"] <= addr and addr + size <= s["vmaddr"] + s["filesize"]:
                return s["fileoff"] + addr - s["vmaddr"]
        raise MatchError("virtual range has no complete file backing")

    def read(self, relative, size):
        return bytes(self.checked(self.file_offset(relative, size), size))

    def section(self, relative):
        addr = self.imagebase + relative
        for s in self.sections:
            if s["addr"] <= addr < s["addr"] + s["size"]:
                return s
        return None

    def identity(self):
        return dict(core_path=str(self.path), arch="arm64", core_sha256=self.core_sha256,
                    arm64_sha256=self.arm64_sha256, module_size=self.module_size,
                    image_size=self.image_size, image_base=self.imagebase, uuid=self.uuid,
                    slice_offset=self.slice_offset, slice_size=self.slice_size)

    def _function_starts(self, off, size):
        raw = self.checked(off, size)
        result, value, shift, accum, terminated = [], 0, 0, 0, False
        for byte in raw:
            accum |= (byte & 127) << shift
            if byte & 128:
                shift += 7
                if shift > 63:
                    raise MatchError("overflow in function starts")
                continue
            if accum == 0:
                terminated = True
                break
            value += accum
            if value % 4 or value >= self.module_size:
                raise MatchError("invalid ARM64 function start")
            self.file_offset(value, 4)
            result.append(value)
            accum = shift = 0
        if not result or not terminated:
            raise MatchError("unterminated/empty function starts")
        return result

    def function_size(self, offset):
        i = bisect.bisect_left(self.function_starts, offset)
        if i >= len(self.function_starts) or self.function_starts[i] != offset:
            raise MatchError(f"0x{offset:x} is not a function boundary")
        sec = self.section(offset)
        end = sec["addr"] + sec["size"] - self.imagebase if sec else self.module_size
        if i + 1 < len(self.function_starts):
            end = min(end, self.function_starts[i + 1])
        if end <= offset or (end - offset) % 4:
            raise MatchError("invalid complete function range")
        return end - offset

    def _chained_fixups(self, off, size):
        if size < 28:
            raise MatchError("truncated chained fixups header")
        version, starts, _, _, _, _, _ = self.unpack("<7I", off)
        if version != 0 or starts + 4 > size:
            raise MatchError("unsupported chained fixups header")
        root = off + starts
        count = self.unpack("<I", root)[0]
        if count != len(self.segments) or starts + 4 + 4 * count > size:
            raise MatchError("invalid chained fixups segment count")
        pointers = {}
        for i in range(count):
            delta = self.unpack("<I", root + 4 + i * 4)[0]
            if not delta:
                continue
            base = root + delta
            if base < off or base + 22 > off + size:
                raise MatchError("truncated chained segment")
            struct_size, page_size, fmt, segment_offset, _, pages = self.unpack("<IHHQIH", base)
            if struct_size < 22 + 2 * pages or base + struct_size > off + size or page_size not in (4096, 16384):
                raise MatchError("invalid chained segment bounds")
            if segment_offset != self.segments[i]["vmaddr"] - self.imagebase:
                raise MatchError("chained segment offset mismatch")
            if fmt not in (1, 2, 6, 9, 12):
                raise MatchError(f"unsupported chained pointer format {fmt}")
            max_entries = (struct_size - 22) // 2
            for page in range(pages):
                first = self.unpack("<H", base + 22 + page * 2)[0]
                if first == 0xFFFF:
                    continue
                offsets = [first]
                if first & 0x8000:
                    offsets = []
                    k = first & 0x7FFF
                    while True:
                        if k >= max_entries:
                            raise MatchError("chained multi-start overflow")
                        entry = self.unpack("<H", base + 22 + k * 2)[0]
                        offsets.append(entry & 0x7FFF)
                        k += 1
                        if entry & 0x8000:
                            break
                for first in offsets:
                    loc = segment_offset + page * page_size + first
                    page_end = segment_offset + (page + 1) * page_size
                    while True:
                        if loc in pointers or loc + 8 > page_end:
                            raise MatchError("invalid/overlapping chained pointer walk")
                        raw = self.unpack("<Q", self.file_offset(loc, 8))[0]
                        if fmt in (2, 6):
                            bind, nxt = raw >> 63, (raw >> 51) & 0xFFF
                            target = (raw & ((1 << 36) - 1)) | (((raw >> 36) & 255) << 56)
                            target = target - self.imagebase if fmt == 2 else target
                            stride = 4
                        else:
                            auth, bind, nxt = raw >> 63, (raw >> 62) & 1, (raw >> 51) & 0x7FF
                            if auth:
                                target = raw & 0xFFFFFFFF
                            else:
                                target = (raw & ((1 << 43) - 1)) | (((raw >> 43) & 255) << 56)
                                if fmt == 1:
                                    target -= self.imagebase
                            stride = 8
                        pointers[loc] = None if bind else target
                        if not nxt:
                            break
                        loc += nxt * stride
        return pointers

    def pointer(self, offset):
        if self.chained_pointers:
            if offset not in self.chained_pointers:
                raise MatchError("vtable slot is not a decoded chained pointer")
            return self.chained_pointers[offset]
        raw = self.unpack("<Q", self.file_offset(offset, 8))[0]
        return raw - self.imagebase if raw else None

    def fingerprint(self, offset):
        if offset in self._fingerprints:
            return self._fingerprints[offset]
        size = self.function_size(offset)
        raw = self.read(offset, size)
        normalized, masks, calls = bytearray(), bytearray(), []
        # Only registers proven to contain an ADRP page may have a page offset
        # normalized. A pointer loaded from that page immediately loses provenance.
        pages = {}
        for i, (word,) in enumerate(struct.iter_unpack("<I", raw)):
            pc = offset + i * 4
            mask = 0xFFFFFFFF
            rd, rn = word & 31, (word >> 5) & 31
            next_page = None
            if word & 0x9F000000 == 0x90000000:  # ADRP
                mask &= ~0x60FFFFE0
                next_page = True
            elif word & 0x9F000000 == 0x10000000:  # ADR
                mask &= ~0x60FFFFE0
            elif word & 0x7C000000 == 0x14000000:  # B or BL
                target = pc + (_signed(word & 0x3FFFFFF, 26) << 2)
                if not offset <= target < offset + size:
                    mask &= ~0x3FFFFFF
                    calls.append({"site": i * 4, "kind": "bl" if word & 0x80000000 else "b", "target": target})
                pages.clear()
            elif word & 0xFF000010 == 0x54000000 or word & 0x7E000000 in (0x34000000, 0x36000000):
                # Conditional control flow invalidates linear ADRP provenance.
                pages.clear()
            elif word & 0x3B000000 == 0x18000000:  # literal load
                mask &= ~0xFFFFE0
            elif word & 0x7F000000 == 0x11000000 and rn in pages:  # ADD immediate page+offset
                mask &= ~0x3FFC00
                pages.pop(rn, None)
            elif word & 0x3B000000 == 0x39000000 and rn in pages:  # unsigned load/store from ADRP page
                mask &= ~0x3FFC00
            # Conservatively invalidate registers on instructions with an Rd.
            # This may miss a relocation but must never invent object provenance.
            pages.pop(rd, None)
            if next_page:
                pages[rd] = True
            normalized.extend(struct.pack("<I", word & mask))
            masks.extend(struct.pack("<I", mask))
        result = dict(offset=offset, size=size, normalized_sha256=_sha(normalized),
                      normalization_mask_sha256=_sha(masks), calls=calls)
        self._fingerprints[offset] = result
        return result

    def incoming_calls(self):
        if self._calls is None:
            self._calls = {}
            # Scan instructions without hashing every function; only relevant
            # callers will later have complete-function fingerprints computed.
            for sec in self.sections:
                if not (sec["flags"] & (0x80000000 | 0x400)):
                    continue
                relative = sec["addr"] - self.imagebase
                raw = self.read(relative, sec["size"] - sec["size"] % 4)
                for i, (word,) in enumerate(struct.iter_unpack("<I", raw)):
                    if word & 0x7C000000 != 0x14000000:
                        continue
                    pc = relative + i * 4
                    target = pc + (_signed(word & 0x3FFFFFF, 26) << 2)
                    if target not in self.function_set:
                        continue
                    k = bisect.bisect_right(self.function_starts, pc) - 1
                    if k < 0:
                        continue
                    caller = self.function_starts[k]
                    if target != caller and pc < caller + self.function_size(caller):
                        self._calls.setdefault(target, []).append((caller, pc - caller))
        return self._calls


def _key(fp):
    return fp["size"], fp["normalized_sha256"], fp["normalization_mask_sha256"]


def _brief(fp):
    return {k: fp[k] for k in ("size", "normalized_sha256", "normalization_mask_sha256")}


def _call_signature(image, fp):
    result = []
    for call in fp["calls"]:
        item = {"site": call["site"], "kind": call["kind"]}
        target = call["target"]
        if target in image.function_set:
            item["function"] = _brief(image.fingerprint(target))
        else:
            # No guessed function boundary: retain the instruction's target
            # relative to its section as an additional conservative constraint.
            section = image.section(target)
            item["non_function"] = {"section": section["name"] if section else None,
                                    "segment": section["segment"] if section else None,
                                    "offset": target - (section["addr"] - image.imagebase) if section else target}
        result.append(item)
    return result


def _entry(image, offset, incoming):
    fp = image.fingerprint(offset)
    # A bounded, deterministic collection of complete caller signatures provides
    # independent disambiguation for short getters without trusting their address.
    callers = []
    for caller, site in incoming.get(offset, []):
        cfp = image.fingerprint(caller)
        if cfp["size"] <= 65536:
            callers.append({"site": site, **_brief(cfp)})
    # Large and distinct callers carry substantially more context than many
    # copies of a tiny template wrapper. Keep one call site per caller body.
    distinct = {}
    for caller in sorted(callers, key=lambda x: (-x["size"], x["normalized_sha256"], x["site"])):
        distinct.setdefault(_key(caller), caller)
    callers = list(distinct.values())[:16]
    return {"offset": offset, **_brief(fp), "calls": _call_signature(image, fp), "callers": callers}


def build_profile(core_path, *, name="wechat-4.1.13-arm64"):
    image = MachOImage(core_path)
    incoming = image.incoming_calls()
    functions = {role: _entry(image, off, incoming) for role, off in DEFAULT_OFFSETS.items()}
    vtables = {}
    for role, offset in DEFAULT_VTABLES.items():
        entries = []
        for i in range(8):
            target = image.pointer(offset + i * 8)
            if target not in image.function_set:
                raise MatchError(f"vtable {role} slot {i} is not a function start")
            entries.append(_entry(image, target, incoming))
        vtables[role] = {"offset": offset, "entries": entries}
    return dict(schema_version=1, profile_id=name, baseline=image.identity(),
                functions=functions, vtables=vtables, layout=dict(DEFAULT_LAYOUT),
                validation_scope="static-full-functions-and-call-relationships-only",
                delivery_verified=False)


def match_profile(core_path, profile):
    """Return a descriptor only after complete, unique static matching.

    Candidate descriptors never confer ABI or send authorization. Invalid binaries
    and ambiguous/changed functions return needs_review, with no descriptor.
    """
    result = dict(schema_version=1, status="needs_review", profile_id=profile.get("profile_id"),
                  identity=None, evidence={"functions": {}, "vtables": {}}, unresolved=[],
                  descriptor=None, validation_scope="static-only", abi_verified=False,
                  delivery_verified=False)
    try:
        if profile.get("schema_version") != 1 or set(profile.get("functions", {})) != set(DEFAULT_OFFSETS) or set(profile.get("vtables", {})) != set(DEFAULT_VTABLES):
            raise MatchError("invalid/incomplete profile roles")
        if profile.get("layout") != DEFAULT_LAYOUT:
            raise MatchError("unrecognized ABI layout; static matching cannot approve it")
        image = MachOImage(core_path)
        result["identity"] = image.identity()
        if not image.uuid:
            raise MatchError("LC_UUID required to verify the loaded image")
        exact = image.core_sha256 == profile["baseline"]["core_sha256"]
        same_slice = image.arm64_sha256 == profile["baseline"]["arm64_sha256"]
        specifications = list(profile["functions"].values()) + [e for table in profile["vtables"].values() for e in table["entries"]]
        keys = {_key(fp) for fp in specifications}
        sizes = {k[0] for k in keys}
        candidates = {k: [] for k in keys}
        if same_slice:
            # Slice hash pins every byte, so baseline addresses are unambiguous.
            for fp in specifications:
                actual = image.fingerprint(fp["offset"])
                if _key(actual) == _key(fp):
                    candidates[_key(fp)].append(fp["offset"])
        else:
            for offset in image.function_starts:
                if image.function_size(offset) in sizes:
                    fp = image.fingerprint(offset)
                    if _key(fp) in keys:
                        candidates[_key(fp)].append(offset)
        incoming = None
        resolved_cache = {}

        def resolve(spec, unique=True):
            nonlocal incoming
            cachekey = (spec["offset"], unique)
            if cachekey in resolved_cache:
                return resolved_cache[cachekey]
            possible = sorted(set(candidates[_key(spec)]))
            if same_slice:
                possible = [x for x in possible if x == spec["offset"]]
            possible = [x for x in possible if _call_signature(image, image.fingerprint(x)) == spec["calls"]]
            if len(possible) > 1 and spec["callers"]:
                if incoming is None:
                    incoming = image.incoming_calls()
                expected = {(c["site"], _key(c)) for c in spec["callers"]}
                filtered = []
                for offset in possible:
                    actual = {(site, _key(image.fingerprint(caller))) for caller, site in incoming.get(offset, [])}
                    matched_bodies = {body for site, body in expected & actual}
                    required = min(2, len({_key(c) for c in spec["callers"]}))
                    if len(matched_bodies) >= required:
                        filtered.append(offset)
                possible = filtered
            resolved_cache[cachekey] = possible
            return possible

        offsets = {}
        for role, spec in profile["functions"].items():
            possible = resolve(spec)
            evidence = dict(baseline_offset=spec["offset"], candidates=possible,
                            full_function_match=bool(possible), direct_calls_match=bool(possible),
                            method="pinned-arm64-sha256" if same_slice else "full-function-call-graph")
            result["evidence"]["functions"][role] = evidence
            if len(possible) == 1:
                offsets[role] = possible[0]
            else:
                result["unresolved"].append(f"function:{role}:{'ambiguous' if possible else 'changed-or-missing'}")
        # Index decoded pointer locations, not raw chained words. Match the whole
        # ordered eight-member table, including short/shared virtual functions.
        pointer_locations = image.chained_pointers
        if not pointer_locations:
            pointer_locations = {}
            for seg in image.segments:
                if seg["initprot"] & 4:
                    continue
                start = seg["vmaddr"] - image.imagebase
                for off in range(start, start + seg["filesize"] - 7, 8):
                    pointer_locations[off] = image.pointer(off)
        vtables = {}
        for role, table in profile["vtables"].items():
            if len(table["entries"]) != 8:
                raise MatchError("vtable needs all eight profiled members")
            member_sets = [set(resolve(e, unique=False)) for e in table["entries"]]
            possible = []
            for location, target in pointer_locations.items():
                if target not in member_sets[0] or (same_slice and location != table["offset"]):
                    continue
                if all(pointer_locations.get(location + j * 8) in choices for j, choices in enumerate(member_sets)):
                    possible.append(location)
            possible.sort()
            result["evidence"]["vtables"][role] = dict(baseline_offset=table["offset"], candidates=possible,
                                                       member_counts=[len(x) for x in member_sets],
                                                       decoded_chained_pointers=bool(image.chained_pointers))
            if len(possible) == 1:
                vtables[role] = possible[0]
            else:
                result["unresolved"].append(f"vtable:{role}:{'ambiguous' if possible else 'changed-or-missing'}")
        if not result["unresolved"]:
            result["status"] = "exact" if exact else "matched"
            result["descriptor"] = {k: result["identity"][k] for k in ("core_path", "arch", "core_sha256", "arm64_sha256", "module_size", "image_size", "uuid")}
            result["descriptor"].update(schema_version=1, profile_id=profile["profile_id"],
                                        offsets=offsets, vtables=vtables, layout=dict(profile["layout"]))
    except (MatchError, OSError, KeyError, TypeError, struct.error, ValueError) as exc:
        result["unresolved"].append(f"invalid-input:{exc}")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "match"))
    parser.add_argument("core")
    parser.add_argument("profile")
    args = parser.parse_args()
    if args.action == "build":
        Path(args.profile).write_text(json.dumps(build_profile(args.core), indent=2) + "\n")
    else:
        print(json.dumps(match_profile(args.core, json.loads(Path(args.profile).read_text())), indent=2))
