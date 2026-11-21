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
| **Fingerprint reader**: enroll, unlock the lock screen, managed in Mobile Settings ([`docs/FINGERPRINT.md`](docs/FINGERPRINT.md)) | ✅ (new) |
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

- **Lock screen:** enabling Phosh's lock screen used to make the display driver crash (screens freeze or go black),
  so automatic locking is **off** by default. Locking/waking worked in recent testing; turn it on in
  Mobile Settings → Surface Duo and report any freeze.
- Camera preview in portrait is obtained by running the camera inside a rotated nested compositor
  (controls end up on the side); direction set in `/etc/duo/camera-rotation`.
- The USB network link to the PC can drop during big transfers (the installer sends data in verified chunks).
- Touch: the outer columns of the left panel pull touches inwards; an edge stretch compensates (`DUO_X_STRETCH_*`).
- Firefox shows a sandbox warning (old 4.14 kernel); Chromium runs with `--no-sandbox`.
- Untested on real hardware yet: USB accessories, GPS outdoor fix, hotspot with a connected client.

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

- **Écran de verrouillage :** il faisait planter le pilote des écrans, le verrouillage automatique est donc désactivé
  par défaut. Il a fonctionné lors des derniers essais : à activer dans Réglages mobiles → Surface Duo.
- **Lecteur d'empreintes :** fonctionne (enregistrement, déverrouillage), voir [`docs/FINGERPRINT.md`](docs/FINGERPRINT.md).
- **Projet open source (GPL-3.0)** : issues, pull requests et tests bienvenus, voir [`CONTRIBUTING.md`](CONTRIBUTING.md).

Ce qui marche, comment ça marche et les remerciements : voir la partie anglaise ci-dessus.
