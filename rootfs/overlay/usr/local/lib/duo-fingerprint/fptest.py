#!/usr/bin/env python3
"""Talk to the Surface Duo's FPC fingerprint trustlet (fpctzrel) from Linux and wait for a finger.
Protocol taken from the Duo's own Android HAL (/vendor/bin/hw/android.hardware.biometrics.fingerprint@2.1-service):
  sensor group 10: 0 init, 1 check/wake, 3 arm finger-down IRQ, 4 capture (3 = retry), 5 sleep."""
import os, select, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qsee import *

DEV = '/sys/bus/platform/devices/soc:fpc1020'

class FPC:
    def __init__(self):
        self.q = os.open('/dev/qseecom', os.O_RDWR)
        self.ion, _ = ion_heaps()
        self.sbfd, self.sb, self.sbva = ion_alloc(self.ion, 27, 1 << 12)
        r = bytearray(struct.pack('i4xQI4x', self.sbfd, self.sbva, 1 << 12))
        fcntl.ioctl(self.q, IOWR(Q, 8, len(r)), r)
        mdt, data, arch = image('fpctzrel')
        ifd, im, _ = ion_alloc(self.ion, 27, (len(data) + 4095) & ~4095); im[:len(data)] = data
        req = bytearray(struct.pack('IIi64sII', len(mdt), len(data), ifd, b'fpctzrel', arch, 0))
        fcntl.ioctl(self.q, IOWR(Q, 7, len(req)), req)
        self.shfd, self.sh, _ = ion_alloc(self.ion, 27, 1 << 12)   # the HAL's 88-byte command area (page)

    def transfer(self, fd=None, size=1 << 12):
        fd = self.shfd if fd is None else fd
        self.sb[:128] = b'\0' * 128; self.sb[:4] = struct.pack('<I', size)
        req = bytearray(struct.pack('<QI4xQI', self.sbva, 64, self.sbva + 64, 64) + struct.pack('<iI', fd, 4) + b'\0' * 28)
        fcntl.ioctl(self.q, IOWR(Q, 4, len(req)), req)
        return struct.unpack_from('<i', self.sb, 64)[0]

    def sensor(self, sub, flag=0):
        for _ in range(50):
            self.sh[:0x58] = b'\0' * 0x58
            self.sh[:8] = struct.pack('<II', 10, sub); self.sh[0xc] = flag
            q = self.transfer()
            st = struct.unpack_from('<i', self.sh, 8)[0]
            if q or st != -12: return q, st, struct.unpack_from('<I', self.sh, 0x10)[0]
            time.sleep(0.01)            # -12 = busy, the HAL retries
        return q, st, None

    def close(self):
        try: self.sensor(5)
        finally: fcntl.ioctl(self.q, IO(Q, 9))

def irqcount():
    for l in open('/proc/interrupts'):
        if 'fpc1020' in l: return sum(int(x) for x in l.split()[1:9])
    return -1

def irq_wait(timeout):
    f = os.open(f'{DEV}/irq', os.O_RDONLY)
    os.read(f, 1); p = select.poll(); p.register(f, select.POLLPRI | select.POLLERR)
    t0 = time.time(); hit = False; c0 = irqcount()
    while time.time() - t0 < timeout:
        if irqcount() != c0: print('  irq count', c0, '->', irqcount(), flush=True); hit = True; break
        if p.poll(250):
            os.lseek(f, 0, 0); v = os.read(f, 4)
            if v.strip() == b'1': hit = True; break
    os.close(f)
    try:
        with open(f'{DEV}/irq', 'w') as w: w.write('1')
    except OSError: pass
    return hit

if __name__ == '__main__':
    tmo = float(sys.argv[1]) if len(sys.argv) > 1 else 20
    for node, val in (('wakeup_enable', 'enable'),):
        try: open(f'{DEV}/{node}', 'w').write(val)
        except OSError as e: print(node, e)
    fp = FPC(); print('trustlet loaded', flush=True)
    try:
        print('init  ->', fp.sensor(0), flush=True)
        print('check ->', fp.sensor(1), flush=True)
        for n in range(3):
            print('arm   ->', fp.sensor(3), flush=True)
            print(f'TOUCH THE SENSOR NOW (waiting {tmo:.0f}s)', flush=True)
            got = irq_wait(tmo)
            print('finger IRQ:', got, flush=True)
            if not got: break
            print('capture ->', fp.sensor(4), flush=True)
            time.sleep(1)
    finally:
        fp.close(); print('closed')
