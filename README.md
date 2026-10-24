# gbl-chainload — fork with stock-fastboot access

GBL/EFISP chainloader for Qualcomm **Snapdragon 8 Gen 5 / 8 Elite (Gen 5)**
devices. It patches the active-slot ABL in memory, installs targeted
protocol hooks, and hands off to the patched ABL.

This fork tracks [1vivy/gbl-chainload](https://github.com/1vivy/gbl-chainload)
and adds:

- **`patch11-fastboot-unlock-verify`** — makes the stock ABL fastboot
  start on OPPO / OnePlus units where the official unlock path
  (`DeepTest` / 深度测试) is unavailable or fails, instead of printing
  `fastboot_unlock_verify error and reboot.` and resetting.
- **`tools/abl_anchor_probe.py`** — classifies that gate on any ABL
  (applicable / already patched / different shape / different string).
- **`docs/PATCH11-PORTING.md`** — how to port it to another model.

> **Tested on one device only: CPH2841 (OPPO Find X9 Ultra), ColorOS
> 17.0.0.101, ABL 16.0.6 on the `abl` partition.** Every other model,
> firmware and OEM is untested.

---

## Why patch11

On units that cannot be unlocked through the official flow, the bootloader
state can read *unlocked* while the stock fastboot still refuses to start:

1. The ABL runs its `fastboot-sec` checks — export whitelist, serial
   match, project id.
2. Verification fails because the unit was not unlocked officially.
3. The ABL prints `fastboot_unlock_verify error and reboot.` and resets
   after a few seconds.

The chainload's own superfastboot still works, and so does `fastbootd`.
What is missing is the stock bootloader's fastboot itself - the ABL
screen and the path flashers take when they push `adb reboot bootloader`
- which is exactly what patch11 restores.

The gate is one branch:

```text
bl   fastboot_unlock_verify      ; x0 = status, 0 = verified
cbz  x0, <after_error>           ; skip the error block on success
adrp x1, ...
add  x1, x1, #<str offset>       ; "fastboot_unlock_verify error and reboot."
...
bl   reboot
<after_error>:
```

`patch11` finds the string, resolves the `ADRP+ADD` that loads it, and
rewrites the `CBZ`/`CBNZ` into an unconditional `B` with the same target.

The patch anchors on the **string**, not on a device id or a fixed
offset. It lives in `abl_permissive`, which is linked into both the host
CLI and the firmware image, so it applies:

- at build time, when packaging a cached ABL (`gbl patch`), and
- at runtime, through the chainload's dynamic-patch path.

It does **not** unlock anything by itself. Unlocking still uses the
chainload's `Enable OEM unlock` / `fastboot flashing unlock` flow;
`patch11` only keeps the ABL from bouncing out of fastboot afterwards.

## Status

| Device | Firmware | ABL on `abl` | Result |
|---|---|---|---|
| CPH2841 (Find X9 Ultra) | ColorOS 17.0.0.101 | 16.0.6 (GBL-capable) | stock fastboot reachable, `DEVICE STATE - unlocked` |

Observations from that single test:

- The `abl` partition must carry a **GBL-capable** ABL. ABLs from
  16.0.10.602 onwards have the `efisp` loader removed and will not start
  the chainload.
- The **cached ABL** inside the efisp payload is a separate artifact and
  should be built from the matching firmware image.
- Mode 2 reached the ABL fastboot with `unlocked: yes`; no userdata
  format was needed.
- Mode 1 also needed **no** userdata format. Its ABL fastboot reports
  `DEVICE STATE - locked` / `unlocked: no` (fakelock), which is expected
  and does not indicate a broken payload.
- Mode 0 was the only payload that failed to boot in this test. It was
  reverted to mode 1 rather than formatted, so whether mode 0 requires a
  format here is unconfirmed.

## Build

```bash
./scripts/build.sh --mode 0    # unlocked observation + preservation baseline
./scripts/build.sh --mode 1    # fakelock (production)
./scripts/build.sh --mode 2    # TA-payload spoof (custom-ROM mode)
cargo test -p patch-engine     # unit tests, incl. patch11 table sizes
```

Artifacts land in `dist/` (EFI) and `bin/` (host tools).

## Use

```bash
# 1. package the efisp payload for the target firmware
python3 scripts/efisp-package.py \
  --abl abl.img --mode 1 \
  --efi dist/gbl-chainload-<ver>.efi \
  --oem oplus \
  --out installed-mode1.efi

# 2. RAM test first — touches no persistent partition
fastboot stage installed-mode1.efi
fastboot oem boot-efi

# 3. once it boots, flash the payload to efisp from the chainload fastboot
#    fastboot flash efisp installed-mode1.efi
```

Enter the ABL fastboot with `adb reboot bootloader` or the chainload menu.

The two fastboot screens read the state differently, and mode-1 makes
that visible:

| Mode | ABL fastboot (stock `START` UI) | chainload superfastboot | `getvar unlocked` (ABL) |
|---|---|---|---|
| 0 | `unlocked` | `unlocked` | `yes` |
| 1 | **`locked`** | `unlocked` | `no` |
| 2 | `unlocked` | `unlocked` | `yes` |

Mode-1 fakelocks the view the ABL reads, so the stock fastboot screen
says `locked`, while the chainload's own fastboot still reports the real
(unlocked) state. Both are expected. What patch11 guarantees is that the
ABL gets this far instead of resetting with
`fastboot_unlock_verify error and reboot.` - a `locked` line on mode-1 is
not a failure, though that mode restricts `flash`/`erase` accordingly.

## Verify the patch

```bash
./bin/gbl patch --check-anchors-only --in abl.efi --oem oplus
./bin/gbl patch --in abl.efi --out patched.efi --oem oplus
```

`applied=4 missed=0` means patch11 found its site alongside patch6,
patch7 and patch10.

To inspect an ABL without patching it:

```bash
python3 tools/abl_anchor_probe.py abl.efi --md report.md --json report.json
# partition images: pass --gbl ./bin/gbl to unwrap on the fly
```

## Porting to another device

The patch is not device-specific. Per build, only three things vary: the
exact string, the guard instruction, and how many loaders reference it.
The probe reports all three and classifies the site as `APPLICABLE`,
`ALREADY_PATCHED`, `B_COND_SHAPE`, `MULTI_ADRL`, `NO_STRING`, …

Short version:

1. `gbl unwrap abl.img out.efi`
2. `python3 tools/abl_anchor_probe.py out.efi` → `APPLICABLE` needs no change
3. `NO_STRING` → add the OEM wording to a `GATE_STRS` slice
4. `B_COND_SHAPE` → confirm polarity, accept `B.cond`
5. `MULTI_ADRL` → patch the loader whose guard is a forward `CBZ`
6. `--check-anchors-only`, then RAM-test with `stage` + `boot-efi`

Full decision table: [docs/PATCH11-PORTING.md](docs/PATCH11-PORTING.md).

## Modes

- **mode-0** — unlocked observation + universal preservation build.
  Installs protocol hooks for logging and the narrow preservation
  baseline: drop TZ soft-fuse advancement and swallow
  `oplusreserve1` / `opporeserve1` writes. VB lock-state and OplusSec
  writes pass through so the stock ABL can run the real relock procedure.
- **mode-1** — protocol-hook fakelock. ABL sees a locked DeviceInfo and
  builds KM `SET_ROT` / `SET_BOOT_STATE` off that view.
- **mode-2** — TA-payload spoof at QSEE/SPSS boundaries (custom-ROM
  mode); ABL stays honest, a per-OTA typed-struct profile is injected
  through `GblPayloadLib`.

## Fastboot surface

Useful getvars: `gbl-chainload_mode`, `gbl-chainload_date`,
`gbl-chainload_auto`, `gbl-chainload_debug`, `gbl-chainload_verbose`,
`unlocked`, `oem-unlock-allowed`, `vbmeta:capabilities`, `vbmeta:slot`,
`vbmeta:warning`, `vbmeta:<partition>:status`.

Useful commands:

- `fastboot stage <file>` — stage an image for `oem boot-efi`
- `fastboot oem escape` — leave the chainload fastboot, continue into
  the patched ABL
- `fastboot oem boot-efi` — `LoadImage()` / `StartImage()` the staged EFI
- `fastboot oem oem-unlock-toggle` — set the FRP OEM-unlock-allowed bit;
  second use is a no-op
- `fastboot flashing lock` / `unlock` — update DevInfo lock state while
  skipping the forced recovery wipe

Menu actions: `Enable OEM unlock`, `Escape`, plus the untested
`Boot ESP` entry.

## Logging

Three primitives from `GblChainloadPkg/Include/Library/GblLog.h`:

| Call | Build flag | Screen | `UefiLog<N>.txt` |
|---|---|---|---|
| `Print(L"...")` | always | yes | yes |
| `GBL_INFO("...")` | `GBL_DEBUG=0` | no | yes |
| `GBL_INFO("...")` | `GBL_DEBUG=1` (`--debug`) | yes | yes |
| `VERBOSE("...")` | `GBL_VERBOSE=0` | stripped | — |
| `VERBOSE("...")` | `GBL_VERBOSE=1` (`--verbose`) | yes | yes |

Use `Print` for prompts and fatal errors, `GBL_INFO` for mutations and
swallows (`vb-fakelock | is_unlocked 1->0`), `VERBOSE` for raw
pass-through traces. All format strings are CHAR8 ASCII.

## Safety

- Test chainload revisions with `fastboot stage` + `fastboot oem boot-efi`
  only. That path touches no persistent partition.
- Do not flash `abl`, `xbl`, `tz` or other boot-chain partitions from the
  chainload fastboot without a recovery path (EDL).
- Keep a dump of the previous `efisp` to roll back.
- `patch11` is **optional**: OPlus ABLs carry the gate and get patched;
  a non-OPlus ABL (Xiaomi and friends) reports a MISS and the run
  continues. Making it mandatory would abort patching on every non-OPlus
  dump.

## Credits and license

The chainloader, patch engine, mode taxonomy and build system are
upstream work by [1vivy](https://github.com/1vivy/gbl-chainload). This
fork adds `patch11`, the anchor probe and the porting notes.

BSD-3-Clause, same as upstream.
