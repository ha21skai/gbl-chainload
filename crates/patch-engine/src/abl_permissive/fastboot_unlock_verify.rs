//! patch11 — fastboot unlock-verify gate.
//!
//! ABL refuses to start fastboot when its unlock verification fails, even
//! on a device whose bootloader state is unlocked: the `fastboot-sec`
//! checks (export whitelist, serial match) fail on an unofficially
//! unlocked unit, the code prints
//!
//! ```text
//! fastboot_unlock_verify error and reboot.
//! ```
//!
//! and resets the device. patch6 opens the lock-state gate but not this
//! one, so fastboot still bounces back to Android.
//!
//! Shape at the call site (CAH/ABL, AArch64):
//!
//! ```text
//! bl   fastboot_unlock_verify     ; x0 = status, 0 = verified
//! cbz  x0, <after_error>          ; skip the error block on success
//! adrp x1, ...
//! add  x1, x1, #<str offset>      ; "fastboot_unlock_verify error and reboot."
//! ...
//! bl   reboot
//! <after_error>:
//! ```
//!
//! Rewriting the `CBZ` to an unconditional `B <after_error>` keeps the
//! fastboot app running regardless of the verification result.
//!
//! This is the same shape as patch6 pattern B, except the guard is a
//! `CBZ`/`CBNZ` instead of `B.NE`. Anything else is reported as a miss:
//! an unknown guard shape must not be patched blind.

use crate::internal::arm64_decode::{decode_branch, find_adrp_add_targeting, Arm64InsnKind};
use crate::internal::encode::{read_instr_u32, rewrite_b_uncond};
use crate::internal::scan::{scan_for, ScanResult};
use crate::PatchOutcome;

/// String printed immediately before the reset.
const GATE_STR: &[u8] = b"fastboot_unlock_verify error and reboot.";

/// patch11 apply function.
pub fn apply(buf: &mut [u8], size: u32) -> PatchOutcome {
    let active = &mut buf[..size as usize];

    // 1. Locate the refusal string. Must be unique.
    let (r, str_off) = scan_for(active, GATE_STR, None);
    match r {
        ScanResult::Found => {}
        ScanResult::Ambiguous => return PatchOutcome::Ambiguous,
        _ => return PatchOutcome::Miss,
    }

    // 2. Resolve the ADRP+ADD pair that loads the string pointer.
    let (r, adrp_off) = find_adrp_add_targeting(active, str_off, true);
    match r {
        ScanResult::Found => {}
        ScanResult::Ambiguous => return PatchOutcome::Ambiguous,
        _ => return PatchOutcome::Miss,
    }

    if adrp_off < 4 {
        return PatchOutcome::Miss;
    }

    // 3. The guard sits right before the ADRP pair.
    let guard_off = adrp_off - 4;
    let guard = read_instr_u32(active, guard_off);

    let decoded = match decode_branch(guard, guard_off) {
        Some(d) => d,
        None => return PatchOutcome::Miss,
    };

    // Only a forward CBZ/CBNZ can be "skip the error block". A backward
    // branch would be a loop; B.cond is patch6's shape and is left alone.
    let is_skip_guard = matches!(
        decoded.kind,
        Arm64InsnKind::CbzW
            | Arm64InsnKind::CbnzW
            | Arm64InsnKind::CbzX
            | Arm64InsnKind::CbnzX
    );
    if !is_skip_guard || decoded.target_off <= adrp_off {
        return PatchOutcome::Miss;
    }

    // 4. CBZ/CBNZ -> B with the same target.
    if !rewrite_b_uncond(active, guard_off, decoded.target_off) {
        return PatchOutcome::Miss;
    }
    PatchOutcome::Ok
}
