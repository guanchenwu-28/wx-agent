"""Offline fixtures only: no process attachment, database access or sends."""
import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adapter_matcher as a


def uleb(value):
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return output


def fixture(code_shift=0, table_shift=0, field=144, duplicate=False, chained=True):
    """43 complete functions and four eight-member pointer tables."""
    n = 43
    begin = 0x1000 + code_shift
    starts = [begin + i * 20 for i in range(n)]
    tables = [0x2100 + table_shift + j * 64 for j in range(4)]
    data = bytearray(0x4000)
    commands = []

    def segment(name, vm, size, fileoff, filesize, prot, section=None):
        cmdsize = 72 + (80 if section else 0)
        cmd = struct.pack('<II16sQQQQIIII', 0x19, cmdsize, name.encode(), vm, size,
                          fileoff, filesize, prot, prot, int(section is not None), 0)
        if section:
            sn, addr, sz, fl = section
            cmd += struct.pack('<16s16sQQIIIIIIII', sn.encode(), name.encode(), addr, sz,
                               addr, 2, 0, 0, fl, 0, 0, 0)
        commands.append(cmd)

    segment('__TEXT', 0, 0x2000, 0, 0x2000, 5,
            ('__text', begin, n * 20, 0x80000400))
    segment('__DATA_CONST', 0x2000, 0x1000, 0x2000, 0x1000, 3)
    segment('__LINKEDIT', 0x3000, 0x1000, 0x3000, 0x1000, 1)
    encoded, previous = bytearray(), 0
    for start in starts:
        encoded.extend(uleb(start - previous))
        previous = start
    encoded.append(0)
    data[0x3100:0x3100 + len(encoded)] = encoded
    commands.append(struct.pack('<IIII', 0x26, 16, 0x3100, len(encoded)))
    commands.append(struct.pack('<II16s', 0x1B, 24, bytes(range(16))))
    if chained:
        # Header, starts-in-image (three segments), starts-in-segment (one page).
        fixups = struct.pack('<7I', 0, 28, 0, 0, 0, 1, 0)
        fixups += struct.pack('<4I', 3, 0, 16, 0)
        fixups += struct.pack('<IHHQIH', 24, 4096, 6, 0x2000, 0, 1)
        fixups += struct.pack('<H', 0x100 + table_shift)
        data[0x3200:0x3200 + len(fixups)] = fixups
        commands.append(struct.pack('<IIII', 0x80000034, 16, 0x3200, len(fixups)))
    cmds = b''.join(commands)
    data[:32] = struct.pack('<8I', 0xFEEDFACF, 0x100000C, 0, 6, len(commands), len(cmds), 0, 0)
    data[32:32 + len(cmds)] = cmds
    for i, offset in enumerate(starts):
        # Unique MOVZ; an OBJECT load whose field offset must remain significant;
        # ADRP + ADD with relocated low page offset; RET.
        marker = 1 if duplicate and i == 1 else i + 1
        words = [0xD2800009 | (marker << 5), 0xF9400020 | ((field // 8) << 10),
                 0xD0000008, 0x91000108 | (((tables[0] & 4095)) << 10), 0xD65F03C0]
        data[offset:offset + 20] = struct.pack('<5I', *words)
    for j, table in enumerate(tables):
        for k in range(8):
            target = starts[11 + j * 8 + k]
            nxt = 2 if (j, k) != (3, 7) else 0
            word = target | (nxt << 51) if chained else target
            struct.pack_into('<Q', data, table + k * 8, word)
    offsets = dict(zip(a.DEFAULT_OFFSETS, starts[:11]))
    vtables = dict(zip(a.DEFAULT_VTABLES, tables))
    return bytes(data), offsets, vtables


def fat(thin, wide=False):
    offset = 4096
    magic = 0xCAFEBABF if wide else 0xCAFEBABE
    arch = struct.pack('>IIQQII', 0x100000C, 0, offset, len(thin), 12, 0) if wide else struct.pack('>IIIII', 0x100000C, 0, offset, len(thin), 12)
    header = struct.pack('>II', magic, 1) + arch
    return header + bytes(offset - len(header)) + thin


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        raw, self.offsets, self.vtables = fixture()
        self.baseline = self.write('baseline', raw)
        self.patches = [patch.object(a, 'DEFAULT_OFFSETS', self.offsets), patch.object(a, 'DEFAULT_VTABLES', self.vtables)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.profile = a.build_profile(self.baseline, name='fixture')

    def write(self, name, data):
        path = Path(self.temp.name) / name
        path.write_bytes(data)
        return path

    def test_exact_roundtrip_and_profile_serializable(self):
        profile = json.loads(json.dumps(self.profile))
        result = a.match_profile(self.baseline, profile)
        self.assertEqual(result['status'], 'exact')
        self.assertEqual(result['descriptor']['offsets'], self.offsets)
        self.assertFalse(result['abi_verified'])
        self.assertFalse(result['delivery_verified'])

    def test_fat32_and_fat64_slice_identity(self):
        thin = self.baseline.read_bytes()
        for wide in (False, True):
            path = self.write(f'fat-{wide}', fat(thin, wide))
            img = a.MachOImage(path)
            self.assertEqual(img.slice_offset, 4096)
            self.assertEqual(img.arm64_sha256, self.profile['baseline']['arm64_sha256'])
            result = a.match_profile(path, self.profile)
            self.assertEqual(result['status'], 'matched')

    def test_functions_and_chained_vtables_relocate(self):
        raw, offsets, tables = fixture(code_shift=0x200, table_shift=0x80)
        result = a.match_profile(self.write('moved', raw), self.profile)
        self.assertEqual(result['status'], 'matched', result['unresolved'])
        self.assertEqual(result['descriptor']['offsets'], offsets)
        self.assertEqual(result['descriptor']['vtables'], tables)

    def test_object_field_change_is_not_masked(self):
        raw, _, _ = fixture(field=152)
        result = a.match_profile(self.write('layout-change', raw), self.profile)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['descriptor'])
        self.assertTrue(any('changed-or-missing' in r for r in result['unresolved']))

    def test_ambiguous_complete_function_rejected(self):
        raw, _, _ = fixture(duplicate=True)
        result = a.match_profile(self.write('duplicate', raw), self.profile)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['descriptor'])
        self.assertTrue(any('ambiguous' in r for r in result['unresolved']))

    def test_truncated_slice_rejected(self):
        for data in (b'\xcf\xfa\xed\xfe', self.baseline.read_bytes()[:-50], fat(self.baseline.read_bytes())[:-10]):
            result = a.match_profile(self.write('short', data), self.profile)
            self.assertEqual(result['status'], 'needs_review')
            self.assertIsNone(result['descriptor'])

    def test_chained_word_must_decode(self):
        image = a.MachOImage(self.baseline)
        at = self.vtables['manager']
        raw = struct.unpack_from('<Q', image.data, at)[0]
        self.assertGreater(raw, image.image_size)
        self.assertEqual(image.pointer(at), 0x1000 + 11 * 20)

    def test_unsupported_chained_format_rejected(self):
        raw = bytearray(self.baseline.read_bytes())
        struct.pack_into('<H', raw, 0x3200 + 28 + 16 + 6, 99)
        result = a.match_profile(self.write('bad-format', raw), self.profile)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn('unsupported chained pointer', result['unresolved'][0])

    def test_truncated_chained_walk_rejected(self):
        raw = bytearray(self.baseline.read_bytes())
        struct.pack_into('<Q', raw, self.vtables['manager'], (4095 << 51) | 0x1000)
        result = a.match_profile(self.write('bad-chain', raw), self.profile)
        self.assertEqual(result['status'], 'needs_review')

    def test_raw_vtables_without_chains(self):
        raw, _, _ = fixture(chained=False)
        path = self.write('raw', raw)
        profile = a.build_profile(path)
        result = a.match_profile(path, profile)
        self.assertEqual(result['status'], 'exact')

    def test_profile_layout_change_cannot_be_approved(self):
        profile = copy.deepcopy(self.profile)
        profile['layout']['recipient'] += 8
        result = a.match_profile(self.baseline, profile)
        self.assertIsNone(result['descriptor'])
        self.assertIn('ABI layout', result['unresolved'][0])

    def test_profile_missing_role_cannot_be_approved(self):
        profile = copy.deepcopy(self.profile)
        profile['functions'].pop('text_factory')
        result = a.match_profile(self.baseline, profile)
        self.assertEqual(result['status'], 'needs_review')

    def test_direct_call_relationship_change_rejected(self):
        # Keep a caller's normalized instruction bytes identical, but change the
        # BL destination to a different complete function.
        raw = bytearray(self.baseline.read_bytes())
        caller = self.offsets['context_factory']
        def change(target):
            struct.pack_into('<I', raw, caller + 8, 0x94000000 | (((target - caller - 8) // 4) & 0x3FFFFFF))
        change(self.offsets['service_getter'])
        baseline = self.write('calls-base', raw)
        profile = a.build_profile(baseline)
        change(self.offsets['executor_getter'])
        target = self.write('calls-changed', raw)
        self.assertEqual(a.MachOImage(baseline).fingerprint(caller)['normalized_sha256'],
                         a.MachOImage(target).fingerprint(caller)['normalized_sha256'])
        result = a.match_profile(target, profile)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn('function:context_factory:changed-or-missing', result['unresolved'])

    def test_chained_binding_cannot_be_function_target(self):
        raw = bytearray(self.baseline.read_bytes())
        at = self.vtables['manager']
        old = struct.unpack_from('<Q', raw, at)[0]
        struct.pack_into('<Q', raw, at, old | (1 << 63))
        result = a.match_profile(self.write('binding-slot', raw), self.profile)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['descriptor'])


if __name__ == '__main__':
    unittest.main()
