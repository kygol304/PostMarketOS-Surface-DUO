# postmarketOS-style Linux for the Microsoft Surface Duo (1st gen)

**Alpine Linux + Phosh on both screens of the Surface Duo 1** — calls, SMS, 4G data, Wi-Fi, Bluetooth,
camera, sensors (hinge postures, auto-rotation, laptop mode) and deep sleep.
Run it **live from RAM** (nothing written to the phone) or **install it** so the Duo boots Linux on its own.

> ⚠️ **Community project, no warranty.** Unlocking the bootloader and installing wipes Android data.
> The Surface Duo 1 has no public EDL loader: a bad flash can brick it for good. Back up all partitions first.

*[Version française plus bas](#français)*

## Screenshots / video

See the [Releases](../../releases) page (demo videos) and [`docs/`](docs/).

## What works

| Feature | Status |
|---|---|
| Both screens (Phosh), apps movable between screens (3-finger swipe, animated) | ✅ |
| Touch on both panels (Matrix heat-map driver), Android-like edge-swipe **Back** | ✅ |
| Hinge postures: closed = screens off, folded = one screen, auto-rotation (4 orientations) | ✅ |
| **Laptop mode**: open in an L shape → app on top, taskbar + full keyboard on the bottom screen (both ways) | ✅ |
| On-screen keyboard (AZERTY/full PC layout) follows the screen of the active app | ✅ |
| Calls (voice through the DSP), SMS | ✅ (call volume buttons: new) |
| **4G mobile data** (custom QMI/IPA data path, ModemManager can't drive the downstream IPA) | ✅ |
| Wi-Fi, Wi-Fi hotspot, Bluetooth (incl. audio) | ✅ (hotspot: lightly tested) |
| Speaker, microphone, volume keys with on-screen indicator, vibration, torch | ✅ |
| Camera (Matrix raw pipeline, ~30 fps), portrait preview | ✅ |
| GPS (modem location service → geoclue) | ✅ (data flowing; outdoor fix not yet confirmed) |
| Deep sleep (also while charging), wake on power key, alarms wake the phone (RTC) | ✅ |
| Auto-brightness (toggle in Settings), French/any locale | ✅ |
| USB-C accessories (host mode switching, USB sticks auto-mounted in `~/USB`) | 🧪 untested with real hardware |
| Firefox, Chromium, GNOME Software (Flathub + Alpine packages) | ✅ |
| Fingerprint reader | ❌ (behind the TrustZone, not reachable from Linux) |
| MMS | ❌ (needs a second data bearer) |

## How it works

- **Kernel:** Microsoft's downstream `msm-4.14` (4.14.190) with a few patches (see [`docs/KERNEL.md`](docs/KERNEL.md)).
  The stock device tree is used; the bootloader applies Microsoft's DTBO.
- **Userspace:** Alpine Linux 3.22 (aarch64) + Phosh 0.47 + a patched **phoc** (touch-to-output mapping,
  3-finger gestures, animated window moves, windows/menus opened on the right screen, keyboard following the app).
- **GPU:** Adreno 640 via KGSL + Mesa Turnip, OpenGL through Zink (Mesa 25.1 built with KGSL).
- **Boot image:** kernel + a tiny initramfs that shows a logo, finds the installed system
  (on `userdata`, or a fallback volume in the free space of `super`) and starts the session.
  Without an installed system it waits for the PC (live mode).

## Install

Requirements: Surface Duo 1 with **unlocked bootloader**, a Linux PC (or Windows + WSL2 + usbipd-win),
`fastboot`, `python3`, a USB-C cable. Full guide: [`docs/INSTALL.md`](docs/INSTALL.md).

```sh
# 1. Live from RAM — nothing is written to the phone (reboot = back to Android)
./installer/duo.sh live

# 2. Install — Android is replaced, Linux boots on its own afterwards
./installer/duo.sh install
```

**Firmware is not included** (it is proprietary). During installation the script copies it **from your own
phone's partitions** (`modem`, `bluetooth`, `vendor`, `persist`), read-only.

> Note: on this phone `fastboot flash` refuses to write the boot partitions ("Device Error").
> The installer writes them **from Linux** with `dd` after checking every byte.

## Back to Android

`./installer/duo.sh restore-android <your boot_b.img backup>` writes the stock boot image back from Linux.
Android then starts from a fresh setup screen.

## Known issues

- **Lock screen:** enabling Phosh's lock screen makes the display driver crash (screens freeze or go black).
  The lock screen is therefore **disabled** for now. Help welcome!
- The camera runs inside a nested compositor whose rotation follows the screen's (portrait, landscape, upside down),
  so the preview stays upright; base direction in `/etc/duo/camera-rotation` (90, or 270 if upside down).
  In that layout the app draws no shutter: tap anywhere on the picture, or press a volume button
  (`duo-camera-declencheur`, an overlay that sends the app's Space key). Photos are then turned the way the phone
  was held (from the accelerometer, `/run/duo/orientation` written by duo-posture).
- The USB network link to the PC can drop during big transfers (the installer sends data in verified chunks).
- Touch: the outer columns of the left panel pull touches inwards; an edge stretch compensates (`DUO_X_STRETCH_*`).
  Panels differ: measure yours with `duo-touch-test A` / `duo-touch-test B` and set stretches/offsets in
  `/etc/duo/touch.conf`.
- Browsers can't use their user-namespace sandbox: the session runs in a chroot, where Linux refuses user namespaces.
  Firefox keeps its seccomp sandbox and no longer shows a warning bar about it (`defaults/pref/duo-sandbox.js`);
  Chromium runs with `--no-sandbox` (everything runs as root) without the warning bar (managed policy).
- Untested on real hardware yet: USB accessories, GPS outdoor fix, hotspot with a connected client.

## Recent fixes

- **Brightness slider** in Phosh's top-bar menu works (`duo-brightness`: Phosh 0.47 only talks to gsd-power,
  which this port doesn't run); Settings → Power now also shows *Automatic Screen Brightness*.
- **Power off / Restart / Log out** from Phosh's menu or a long press on Power work (`duo-session-manager`,
  a minimal `org.gnome.SessionManager`; there is no gnome-session).
- **Flatpak apps** installed from GNOME Software / Flathub show up in the app grid (`XDG_DATA_DIRS`) and start:
  the session is chrooted and Linux refuses user namespaces inside a chroot, so `duo-bwrap` (`FLATPAK_BWRAP`) runs
  bubblewrap without `--unshare-user` (root needs none for the rest of the sandbox). Electron apps also get
  `ELECTRON_DISABLE_SANDBOX=1` and Wayland (`/var/lib/flatpak/overrides/global`); older Electron builds may still
  need `--ozone-platform=wayland` on their launcher line. Tested with FreeTube.
- **Rotation** reacts faster: 0.35 s hold instead of 0.8 s, at a slightly smaller tilt (`/etc/duo/rotation.conf`).
- **Touch**: per-panel calibration file and test tool (above); taps register ~20 ms sooner (`DUO_QUIET_MS`).
- **Bigger UI option**: `/etc/duo/scale` sets the interface scale (default 2; 2.75–3 gives finger-sized buttons
  and icons). phoc's config is generated from it at login, and duoheat/duo-posture follow it.
- **Home bar**: only touches starting near the white handle trigger it; elsewhere along the bottom edge they go to
  the app (`DUO_HOME_HALF_WIDTH` in `/etc/duo/touch.conf`, 0 = whole bar as before).
- **Camera**: English strings (`sprache/en.tsv`; the French ones moved to `fr.tsv`).
- **GPU** minimum clock 345 MHz instead of 257 MHz, so animations and scrolling start faster.

## Contributing

This port is **open source** (GPL-3.0-or-later) and contributions are welcome: issues, pull requests, testing.
See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`docs/BUILD.md`](docs/BUILD.md).

## Credits

- [Matrix-Hardline/matrix-duo](https://github.com/Matrix-Hardline/matrix-duo) (GPL-3.0) — touch heat-map driver,
  camera, Bluetooth, sensors and audio recipes, kernel and Mesa patches. This port would not exist without it.
- Microsoft — [surface-duo-oss-kernel.msm-4.14](https://github.com/microsoft/surface-duo-oss-kernel.msm-4.14) (GPL-2.0)
- Droidian Surface Duo recipe (alghiffaryfa19), postmarketOS / pmbootstrap, Alpine Linux, Phosh/phoc (Purism, GNOME),
  linux-msm (qrtr, rmtfs, pd-mapper, tqftpserv), hexagonrpc & libssc, Mesa.

Full list with licences: [`CREDITS.md`](CREDITS.md). Our own code: **GPL-3.0-or-later** ([`LICENSE`](LICENSE)).

---

## Français

**Alpine Linux + Phosh sur les deux écrans du Surface Duo 1** : appels, SMS, données 4G, Wi-Fi, Bluetooth,
caméra, capteurs (postures de la charnière, rotation auto, mode PC portable) et veille profonde.
À essayer **en mémoire vive** (rien n'est écrit sur le téléphone) ou à **installer** pour que le Duo démarre seul sur Linux.

> ⚠️ **Projet communautaire, sans garantie.** L'installation remplace Android et efface ses données.
> Le Surface Duo 1 n'a pas de chargeur EDL public : sauvegardez toutes les partitions avant.

- **Essayer sans rien installer :** `./installer/duo.sh live` (au redémarrage, retour sous Android).
- **Installer :** `./installer/duo.sh install`. Les micrologiciels propriétaires ne sont pas fournis : ils sont copiés
  depuis les partitions de **votre** téléphone, en lecture seule.
- **Revenir à Android :** `./installer/duo.sh restore-android <sauvegarde boot_b.img>`.

- **Problème connu :** activer l'écran de verrouillage de Phosh fait planter le pilote des écrans ; il est désactivé
  pour l'instant. Toute aide est bienvenue.
- **Projet open source (GPL-3.0)** : issues, pull requests et tests bienvenus, voir [`CONTRIBUTING.md`](CONTRIBUTING.md).

Ce qui marche, comment ça marche et les remerciements : voir la partie anglaise ci-dessus.
