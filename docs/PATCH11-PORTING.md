# Porting patch11 to another device

`patch11-fastboot-unlock-verify` anchors on a **string**, not on a device
ID or a file offset. Nothing in the patch mentions `CPH2841`, so it is
not device-specific: any ABL that carries

```
fastboot_unlock_verify error and reboot.
```

with the same guard shape is patched by the same code. All X9 Ultra
builds checked so far (`16.0.6`, `17.0.0.100`, `17.0.0.101`) resolve to
the same site.

The only per-build variables are:

1. the exact string (localisation, OEM wording),
2. the guard instruction in front of the `ADRP+ADD` loader,
3. how many loaders reference the string.

`abl_anchor_probe.py` reports all three.

## Workflow

```bash
# raw PE (already unwrapped)
python3 abl_anchor_probe.py abL.efi --md report.md --json report.json

# partition image (needs gbl on PATH or --gbl)
python3 abl_anchor_probe.py abl.img --gbl ./bin/gbl
```

Verdicts:

| Verdict | Meaning | Action |
|---|---|---|
| `APPLICABLE` | unique string, unique loader, forward `CBZ`/`CBNZ` | patch applies as-is |
| `ALREADY_PATCHED` | guard is already an unconditional `B` | nothing to do |
| `B_COND_SHAPE` | guard is `B.NE` (patch6's pattern B) | see below |
| `BACKWARD_BRANCH` | guard targets backwards | hand-review; not a skip-over-error |
| `MULTI_ADRL` | several loaders for the string | see below |
| `SHAPE_UNKNOWN` / `NO_ADRL` | guard is not a recognised branch | hand-review in a disassembler |
| `NO_STRING` | different ABL family | add the OEM string as a variant |
| `STRING_AMBIGUOUS` | string appears twice | restrict to `.text` or add context bytes |

## Extending for a new shape

**`B_COND_SHAPE`** — if the build guards with `b.ne` instead of `cbz`,
the "always skip the error" rewrite is the same `B` with the same
target, but you must confirm the condition's polarity in a disassembler
first. Add the case next to the `CBZ` match in
`fastboot_unlock_verify.rs`.

**`MULTI_ADRL`** — walk every canonical `ADRP+ADD` pair whose target is
the string and patch the one whose guard is a forward `CBZ`/`CBNZ`.
`find_adrp_add_targeting()` refuses on ambiguity by design, so this case
needs a small loop over `decode_adrp_add()` instead.

**`NO_STRING`** — other OEMs phrase the refusal differently (or the
string lives in a wide/UTF-16 pool). Add the variant to a `const
GATE_STRS: &[&[u8]]` slice and loop over it, mirroring `patch6`'s
`REFUSAL_STRS`. Keep each variant unique; a variant that matches twice
returns `Ambiguous` on purpose.

## Testing a port

```bash
cargo test -p patch-engine
scripts/build.sh                       # docker; rebuilds gbl + EFI
./bin/gbl patch --check-anchors-only --in abl.efi --oem oplus
./bin/gbl patch --in abl.efi --out patched.efi --oem oplus
```

`applied=4 missed=0` means patch11 found its site. On the device, use
the RAM path only:

```bash
fastboot stage dist/gbl-chainload-<ver>.efi
fastboot oem boot-efi
```

Expected screen: the ABL fastboot UI with `DEVICE STATE - unlocked` on a
unit that was unlocked unofficially, and no
`fastboot_unlock_verify error and reboot.`

## Device coverage notes

- The patch is `mandatory: true`. On an ABL without the string the CLI
  reports `worst=2`; the firmware path treats it as a mandatory miss.
  Make it `false` if you want a soft failure on unknown ABLs.
- The `ABL_PERMISSIVE` scope is linked into both the host CLI and the
  firmware image, so the runtime dynamic-patch path applies it too — a
  bare efisp (no cached ABL) benefits as well, as long as the ABL on the
  `abl` partition carries the string.
