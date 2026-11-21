# Building from source

Everything here was built on Debian (WSL2) with an aarch64 cross toolchain and `pmbootstrap`'s `apk.static`.

| Part | Where | How |
|---|---|---|
| Kernel 4.14.190 | [microsoft/surface-duo-oss-kernel.msm-4.14](https://github.com/microsoft/surface-duo-oss-kernel.msm-4.14), Droidian recipe | config `kernel/config-4.14-190-surfaceduo`, patches in `kernel/` (`kernel-waitid-P_PIDFD.patch` is required, otherwise phoc kills phosh) |
| Boot image | `boot/` | `boot/mkboot.sh` : stock DTB + kernel + initramfs (`boot/init`, `auto-snippet.sh`, `start.sh`, logo) — header v2 |
| Root filesystem | `rootfs/` | Alpine 3.22 aarch64 (`mkroot.sh`), packages in `packages.txt`, then `overlay/` copied on top (`pack.sh`) |
| phoc 0.47 | Alpine sources + `phoc/*.py` | patch scripts are idempotent; `phoc/build-phoc.sh` shows the build. Also apply the wlroots patches from phoc's `subprojects/packagefiles` |
| Mesa 25.1.9 | Mesa + `kernel/mesa-25.1-turnip-kgsl-*.patch` | Turnip with the KGSL backend; OpenGL through Zink (`/etc/duo/gpu.env`) |
| Tools | `tools/*.c` | `aarch64-linux-gnu-gcc -static -O2 -o name name.c` |
| Sensors | hexagonrpc + libssc with Matrix's downstream patches | see Matrix's `system/bau-hexagonrpc-42.sh`, `bau-libssc-42.sh` |
| Wi-Fi | `wlan.ko` (qcacld) rebuilt with `IPA_OFFLOAD=n`; linux-msm qrtr, rmtfs **read-only** (`-r`), pd-mapper, tqftpserv | |

## Important pieces of the overlay

- `usr/local/bin/duo-phosh`, `duo-dans-session` — session start (with automatic restart if the compositor crashes)
- `usr/local/sbin/duo-posture` — hinge postures, auto-rotation, laptop mode
- `usr/local/sbin/duo-donnees` + `tools/duo-ipa-data.c` — 4G data path (DPM open-port, WDA QMAP, WDS bind mux)
- `usr/local/sbin/duo-veille` — deep sleep (pauses audio and USB, RTC wake for alarms)
- `usr/local/sbin/duo-usb-hote` — USB-C host/peripheral switching, USB stick mounting
- `usr/local/bin/duo-retour`, `duo-ecrans` — edge-swipe Back, 3-finger window moves (lisgd)
- `usr/local/bin/duo-brightness`, `duo-session-manager` — backends for Phosh's brightness slider and power menu
- `usr/local/bin/duo-touch-test`, `etc/duo/touch.conf` — touch accuracy test and per-panel calibration (read by `tools/duoheat.c`)

Comments in the scripts are in French; issues and pull requests in English or French are welcome.
