#!/bin/sh
# Build and install the Mobile Settings "Fingerprint & Lock" panel (run on the phone, in the Linux root)
set -e
cd "$(dirname "$0")"
/usr/bin/gcc -shared -fPIC -O2 -Wall -o libms-plugin-duo-fingerprint.so ms-plugin-duo-fingerprint.c \
    $(pkg-config --cflags --libs gtk4 libadwaita-1)
install -Dm644 libms-plugin-duo-fingerprint.so /usr/lib/phosh-mobile-settings/plugins/libms-plugin-duo-fingerprint.so
echo BUILD_OK
