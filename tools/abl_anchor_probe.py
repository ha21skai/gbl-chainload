#!/usr/bin/env python3
"""Probe an ABL PE for the fastboot_unlock_verify gate (patch11) and the
neighbouring patch anchors.

Answers three questions for any OPlus-family ABL:

  1. Is the gate string present, and unique?
  2. Does its ADRP+ADD loader resolve uniquely?
  3. What instruction guards it, and can patch11 rewrite it?

Porting to a new device normally means: run this, look at the verdict,
and only if the shape differs (B.NE, backward branch, second loader)
extend the patch or hand-review the site.

Usage:
  abl_anchor_probe.py <abl.efi | abl.img> [more ...] [--json out.json]
                      [--md out.md] [--apply out.efi] [--string STR]
                      [--gbl PATH]

A raw PE32+ is used as-is. A partition image (ELF/FV) is unwrapped with
`gbl unwrap` when --gbl (or gbl.exe on PATH) is available.
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

try:
    from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pylibs'))
    from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM

DEFAULT_STRING = b'fastboot_unlock_verify error and reboot.'
CS = Cs(CS_ARCH_ARM64, CS_MODE_ARM)

NOP = 0xD503201F


def sign_extend(v, bits):
    if v & (1 << (bits - 1)):
        v -= 1 << bits
    return v


def decode_adrp(insn, off):
    if (insn & 0x9F000000) != 0x90000000:
        return None
    imm = (((insn >> 5) & 0x7FFFF) << 2) | ((insn >> 29) & 3)
    return ((off & ~0xFFF) + (sign_extend(imm, 21) << 12)) & 0xFFFFFFFF, insn & 0x1F


def decode_add_imm(insn):
    if (insn & 0x7F800000) != 0x11000000:
        return None
    return (insn >> 10) & 0xFFF, (insn >> 5) & 0x1F, insn & 0x1F


def decode_branch(insn, off):
    top = (insn >> 24) & 0xFF
    if top in (0x34, 0x35, 0xB4, 0xB5):
        imm = sign_extend((insn >> 5) & 0x7FFFF, 19)
        kind = {0x34: 'cbz.w', 0x35: 'cbnz.w', 0xB4: 'cbz.x', 0xB5: 'cbnz.x'}[top]
        return kind, (off + imm * 4) & 0xFFFFFFFF
    if top == 0x54 and (insn & 0x10) == 0:
        imm = sign_extend((insn >> 5) & 0x7FFFF, 19)
        return 'b.cond(#%d)' % (insn & 0xF), (off + imm * 4) & 0xFFFFFFFF
    if (insn >> 26) == 0x05:
        imm = sign_extend(insn & 0x3FFFFFF, 26)
        return 'b', (off + imm * 4) & 0xFFFFFFFF
    return None


def is_pe(data):
    return data[:2] == b'MZ'


def unwrap(img, gbl):
    # Keep the scratch dir next to the caller: some sandboxes deny writes
    # to the system temp directory for child processes.
    tmp = tempfile.mkdtemp(prefix='probe-', dir=os.getcwd())
    out = os.path.join(tmp, 'out.efi')
    try:
        r = subprocess.run([gbl, 'unwrap', img, out], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            raise SystemExit('gbl unwrap failed for %s:\n%s%s' % (img, r.stdout, r.stderr))
        return open(out, 'rb').read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def find_refs(data, str_off):
    refs = []
    for off in range(0, len(data) - 8, 4):
        a = decode_adrp(struct.unpack_from('<I', data, off)[0], off)
        b = decode_add_imm(struct.unpack_from('<I', data, off + 4)[0])
        if a and b and b[1] == a[1] and b[2] == a[1]:
            if ((a[0] + b[0]) & 0xFFFFFFFF) == str_off:
                refs.append(off)
    return refs


def disasm_one(data, off, count=1):
    if off < 0 or off + 4 * count > len(data):
        return ''
    return '; '.join('%s %s' % (i.mnemonic, i.op_str)
                     for i in CS.disasm(data[off:off + 4 * count], off))


def probe(data, needle):
    res = {'size': len(data), 'string': None, 'refs': [], 'verdict': 'NO_STRING', 'guard': None}
    str_off = data.find(needle)
    if str_off < 0:
        return res
    res['string'] = str_off
    if data.find(needle, str_off + 1) >= 0:
        res['verdict'] = 'STRING_AMBIGUOUS'
        return res

    refs = find_refs(data, str_off)
    res['refs'] = refs
    if not refs:
        res['verdict'] = 'NO_ADRL'
        return res
    if len(refs) > 1:
        # multiple loaders: report but let the caller judge
        res['verdict'] = 'MULTI_ADRL'

    for ref in refs:
        if ref < 4:
            continue
        guard = struct.unpack_from('<I', data, ref - 4)[0]
        dec = decode_branch(guard, ref - 4)
        entry = {
            'ref': ref,
            'guard_off': ref - 4,
            'guard_hex': '%08x' % guard,
            'guard_asm': disasm_one(data, ref - 4, 1),
            'kind': dec[0] if dec else None,
            'target': dec[1] if dec else None,
        }
        res['refs'][res['refs'].index(ref)] = entry
        if dec and dec[0].startswith(('cbz', 'cbnz')):
            fwd = dec[1] > ref
            entry['verdict'] = 'APPLICABLE' if fwd else 'BACKWARD_BRANCH'
        elif dec and dec[0] == 'b':
            entry['verdict'] = 'ALREADY_PATCHED'
        elif dec and dec[0].startswith('b.cond'):
            entry['verdict'] = 'B_COND_SHAPE'
        else:
            entry['verdict'] = 'SHAPE_UNKNOWN'

    verdicts = [e.get('verdict') for e in res['refs'] if isinstance(e, dict)]
    for pref in ('APPLICABLE', 'ALREADY_PATCHED', 'B_COND_SHAPE', 'BACKWARD_BRANCH', 'SHAPE_UNKNOWN'):
        if pref in verdicts:
            res['verdict'] = pref
            break
    return res


def apply_patch(data, needle):
    """Return (patched_bytes, report) applying the patch11 rewrite."""
    data = bytearray(data)
    r = probe(bytes(data), needle)
    if r['verdict'] != 'APPLICABLE':
        return None, r
    changed = []
    for e in r['refs']:
        if isinstance(e, dict) and e.get('verdict') == 'APPLICABLE':
            target = e['target']
            off = e['guard_off']
            delta = (target - off) >> 2
            insn = 0x14000000 | (delta & 0x03FFFFFF)
            struct.pack_into('<I', data, off, insn)
            changed.append((off, e['guard_hex'], '%08x' % insn))
    r['applied'] = changed
    return bytes(data), r


def fmt(res, name):
    lines = []
    lines.append('## %s' % name)
    lines.append('')
    lines.append('| item | value |')
    lines.append('|---|---|')
    lines.append('| PE size | %d |' % res['size'])
    lines.append('| string | %s |' % ('0x%x' % res['string'] if res['string'] is not None else 'absent'))
    lines.append('| verdict | **%s** |' % res['verdict'])
    for e in res['refs']:
        if not isinstance(e, dict):
            continue
        lines.append('| guard @0x%x | `%s` (%s) -> 0x%x |' % (
            e['guard_off'], e['guard_asm'], e['kind'], e['target'] if e['target'] is not None else 0))
        lines.append('| -> verdict | %s |' % e.get('verdict'))
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('--string', default=None, help='override the anchor string')
    ap.add_argument('--json', default=None)
    ap.add_argument('--md', default=None)
    ap.add_argument('--apply', default=None, help='write the patched PE (single input)')
    ap.add_argument('--gbl', default=None, help='gbl binary for .img inputs')
    args = ap.parse_args()

    needle = args.string.encode() if args.string else DEFAULT_STRING
    gbl = args.gbl or shutil.which('gbl') or shutil.which('gbl.exe')
    results = []
    md = []

    for path in args.inputs:
        raw = open(path, 'rb').read()
        if not is_pe(raw):
            if not gbl:
                print('%s: not a PE and no gbl available to unwrap' % path, file=sys.stderr)
                continue
            raw = unwrap(path, gbl)
        res = probe(raw, needle)
        res['input'] = path
        results.append(res)
        md.append(fmt(res, os.path.basename(path)))

        print('%-40s %-16s string=%s refs=%d' % (
            os.path.basename(path), res['verdict'],
            ('0x%x' % res['string']) if res['string'] is not None else '-',
            len([r for r in res['refs'] if isinstance(r, dict)])))

        if args.apply and len(args.inputs) == 1:
            patched, rep = apply_patch(raw, needle)
            if patched is None:
                print('  not applicable: %s' % rep['verdict'], file=sys.stderr)
            else:
                open(args.apply, 'wb').write(patched)
                for off, before, after in rep['applied']:
                    print('  patched 0x%x: %s -> %s' % (off, before, after))
                print('  wrote %s' % args.apply)

    if args.md:
        open(args.md, 'w', encoding='utf-8').write('\n\n'.join(md) + '\n')
    if args.json:
        open(args.json, 'w', encoding='utf-8').write(json.dumps(results, indent=2, default=str))


if __name__ == '__main__':
    main()
