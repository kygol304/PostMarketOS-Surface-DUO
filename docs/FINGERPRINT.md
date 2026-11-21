# Fingerprint reader

The Duo's FPC fingerprint sensor works from Linux: enrolling, matching, saving fingerprints across reboots, and
unlocking Phosh's lock screen. Managed from **Mobile Settings → Surface Duo** or the `duo-fingerprint` command.

## Using it

- **Mobile Settings → Surface Duo**: automatic brightness on/off, unlock with fingerprint on/off, lock when the screen turns off on/off,
  the list of enrolled fingers (remove with the bin icon), **Add fingerprint** (progress shown on the page and as a
  notification; touch, lift and repeat about 15–20 times).
- The lock screen PIN is the user's Unix password (`passwd`). It always works, fingerprint or not.
- Terminal equivalent:

```
duo-fingerprint list            show enrolled fingers (up to 5)
duo-fingerprint enroll          add a finger (prompts on screen)
duo-fingerprint delete ID|all   remove a finger
duo-fingerprint unlock on|off   fingerprint on the lock screen
duo-fingerprint autolock on|off lock automatically when the screen turns off
duo-fingerprint status          settings and the last log lines (/var/log/duo-fpunlock.log)
```

## How it works

The sensor's SPI bus belongs to TrustZone; Linux only drives its power, reset and IRQ (`fpc1020` platform driver).
Everything else goes through Qualcomm's QSEECOM interface (`/dev/qseecom` + ION), which the 4.14 kernel has.
No Android code or binaries run: the protocols below were worked out from the Duo's own Android HAL and from
Qualcomm's open-source MinkIPC listeners (BSD-3-Clause-Clear), and reimplemented in Python.

1. **TrustZone listeners** (`qlisten.py`, a small stand-in for Android's `qseecomd`). Trusted apps call back into the
   normal world for storage; without these, gatekeeper refuses to work (`VERIFICATION_FAILED`) and fingerprints
   can't be saved:
   - `0xa` file service and `0x7000` GP file service: files under `/var/lib/duo-tz` (TrustZone's paths prefixed).
     The FPC app uses the *old* GP command set with absolute paths (0 read, 1 write).
   - `0x2000` RPMB: the UFS RPMB well-known LUN (`0:0:0:49476`) through SG_IO (`rpmb.py`). The 4.14 kernel has no
     `ufs-bsg`, so the size (16 MiB) was measured with unauthenticated reads.
   - `0xb` time.
2. **FPC trusted app** `fpctzrel` (already copied to `/lib/firmware` from the modem partition by the installer),
   loaded with `QSEECOM_IOCTL_LOAD_APP_REQ`. Commands go in an ION buffer `{group, cmd, status, ...}` with
   `SEND_MODFD_CMD`.
3. **keymaster64** (already loaded by TrustZone at boot): version handshake (`0x200`/`0x207`), HMAC agreement
   (`0x20E`/`0x20F`), then `GET_AUTH_TOKEN_KEY` (`0x205`) — only once the FPC app is loaded, since the key comes back
   wrapped for it. The key is handed to the FPC app.
4. **Gatekeeper** (inside keymaster64, legacy structs `0x1001` enroll / `0x1002` verify): issues the signed
   69-byte hardware auth token the FPC app requires before it accepts a new finger.
5. **FPC protocol**: sensor group 10 (init, wait finger up, arm IRQ, capture, sleep), group 11 enroll/identify,
   group 2 database load/store (`/data/vendor_de/0/fpdata/user.db`, the names the app insists on), group 3 auth.

**Unlocking:** Phosh 0.47 has no fingerprint support, but it unlocks when logind sends `Unlock`. `duo-pam-session`
starts the Phosh session inside an elogind session (`pam_elogind`); `duo-fpunlock` keeps the FPC app loaded, arms the
sensor while the session's `LockedHint` is set, and runs `loginctl unlock-session` on a match. `/etc/pam.d/phosh`
makes the lock screen PIN pad work (`pam_unix`).

## Files

| Path | What |
|---|---|
| `usr/local/lib/duo-fingerprint/` | `qsee.py` QSEECOM/ION client, `km.py` keymaster + gatekeeper, `rpmb.py`, `qlisten.py` listeners, `fptest.py` sensor/IRQ, `fp2.py` enroll/identify/list/delete tool |
| `usr/local/bin/duo-fpunlock` | unlock daemon (started by `duo-phosh`); off switch `/etc/duo/fingerprint-unlock-off` |
| `usr/local/bin/duo-fingerprint` | command-line management |
| `usr/local/bin/duo-pam-session`, `etc/pam.d/duo-session` | runs the session inside elogind |
| `etc/pam.d/phosh` | lock screen PIN |
| `tools/ms-plugin-duo-fingerprint/` | Mobile Settings panel (GIO module, `ms-device-panel` extension point) |
| `/etc/duo/autolock` | present = lock when the screen turns off (read by `duo-phosh`/`duo-dans-session`) |

The Mobile Settings panel is a shared GIO module, so it can't be built statically like the other tools. Build it on
the phone (or in the rootfs chroot) with `gtk4.0-dev libadwaita-dev gcc musl-dev pkgconf` installed:
`sh tools/ms-plugin-duo-fingerprint/build.sh` (installs into `/usr/lib/phosh-mobile-settings/plugins/`). Note: on the
4.14 kernel `gcc` must be called by its full path (`/usr/bin/gcc`), otherwise it can't find `cc1`.

## Status / limits

- Tested on one Duo 1 (bootloader 2022.815.527): enroll, save, reload after reboot, enrolled finger accepted,
  other fingers rejected, fingerprint and PIN unlock.
- Untested: fingerprint unlock right after waking from deep sleep; more than one enrolled finger.
- Automatic lock is **off** by default (the lock screen was disabled earlier because of display-driver crashes;
  none were seen when locking and waking in testing). Turn it on in Mobile Settings.
- Writes to RPMB happen only through TrustZone (gatekeeper/keymaster records), as on Android.
