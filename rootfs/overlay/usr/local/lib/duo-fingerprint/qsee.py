#!/usr/bin/env python3
"""Minimal QSEECom client (no Android libs): list ION heaps, query/load a trusted app, send a raw command.
   usage: qsee.py heaps | query NAME... | load NAME [cmdhex...]"""
import ctypes, errno, fcntl, glob, mmap, os, struct, sys
def IOWR(t, nr, size): return (3 << 30) | (size << 16) | (t << 8) | nr
def IO(t, nr): return (t << 8) | nr
Q = 0x97; I = ord('I')
FW = '/lib/firmware'

def ion_heaps():
    ion = os.open('/dev/ion', os.O_RDWR)
    q = bytearray(struct.pack('IIIIQ', 0, 0, 0, 0, 0))  # ion_heap_query {cnt, reserved0, heaps(u64) ...}
    q = bytearray(struct.pack('IIQII', 0, 0, 0, 0, 0))
    fcntl.ioctl(ion, IOWR(I, 8, len(q)), q)
    cnt = struct.unpack_from('I', q)[0]
    arr = ctypes.create_string_buffer(cnt * 52)  # ion_heap_data {char name[32]; u32 type, heap_id, r0, r1, r2}
    q = bytearray(struct.pack('IIQII', cnt, 0, ctypes.addressof(arr), 0, 0))
    fcntl.ioctl(ion, IOWR(I, 8, len(q)), q)
    out = []
    for i in range(cnt):
        name, typ, hid = struct.unpack_from('32sII', arr.raw, i * 52)
        out.append((name.rstrip(b'\0').decode(), typ, hid))
    return ion, out

def ion_alloc(ion, heap, size):
    a = bytearray(struct.pack('QIIII', size, 1 << heap, 0, 0, 0))
    fcntl.ioctl(ion, IOWR(I, 0, len(a)), a)
    fd = struct.unpack_from('QIIII', a)[3]
    m = mmap.mmap(fd, size)
    return fd, m, ctypes.addressof(ctypes.c_char.from_buffer(m))

def query(qfd, name):
    b = bytearray(struct.pack('64sII', name.encode(), 0, 0))
    try:
        fcntl.ioctl(qfd, IOWR(Q, 15, len(b)), b)
        return 'not loaded'
    except OSError as e:
        if e.errno == errno.EEXIST:
            return 'LOADED id=%d' % struct.unpack_from('64sII', b)[1]
        return 'error %s' % errno.errorcode.get(e.errno)

def image(name):
    mdt = open(f'{FW}/{name}.mdt', 'rb').read()
    parts = sorted(glob.glob(f'{FW}/{name}.b[0-9][0-9]'))
    data = mdt + b''.join(open(p, 'rb').read() for p in parts)
    return mdt, data, 2 if mdt[4] == 2 else 1

def fpc_cmd(qfd, ion, sbm, sbva, group, cmd, size, payload=b''):
    # FPC TAC transport (from the Duo's HAL): ION buffer {u32 group, u32 cmd, i32 status, ...}; 64-byte command
    # {u32 len, u32 phys (filled by the kernel)} and 64-byte response {u32 status}, sent with SEND_MODFD_CMD.
    size = max(size, 16 + len(payload))
    fd, m, _ = ion_alloc(ion, 27, (size + 4095) & ~4095)
    m[:16] = struct.pack('<IIiI', group, cmd, 0, 0); m[16:16 + len(payload)] = payload
    if not payload: m[0x10:0x14] = struct.pack('<I', size - 0x14)
    sbm[:128] = b'\0' * 128; sbm[:4] = struct.pack('<I', (size + 4095) & ~4095)
    req = bytearray(struct.pack('<QI4xQI', sbva, 64, sbva + 64, 64) + struct.pack('<iI', fd, 4) + b'\0' * 24 + b'\0' * 4)
    try:
        fcntl.ioctl(qfd, IOWR(Q, 4, len(req)), req)
    except OSError as e:
        return 'ioctl error %s (req %d bytes)' % (errno.errorcode.get(e.errno), len(req))
    st = struct.unpack_from('<i', sbm, 64)[0]; ta = struct.unpack_from('<i', m, 8)[0]
    body = bytes(m[0x14:size]).split(b'\0')[0]
    return 'qsee=%d ta=%d hdr=%s body=%r' % (st, ta, bytes(m[:0x14]).hex(), body[:300])

if __name__ == '__main__':
    qfd = os.open('/dev/qseecom', os.O_RDWR)
    cmd = sys.argv[1]
    if cmd == 'heaps':
        for h in ion_heaps()[1]: print(h)
    elif cmd == 'query':
        for n in sys.argv[2:]: print(n, query(qfd, n))
    elif cmd == 'load':
        name = sys.argv[2]
        ion, heaps = ion_heaps()
        hid = {n: i for n, t, i in heaps}
        heap = next((hid[n] for n in ('qsecom_ta', 'qsecom') if n in hid), None)
        print('heaps', heaps, 'using', heap)
        sbfd, sbm, sbva = ion_alloc(ion, hid.get('qsecom', heap), 1 << 16)
        r = bytearray(struct.pack('iIQI4x', sbfd, 0, 0, 0))
        r = bytearray(struct.pack('i4xQI4x', sbfd, sbva, 1 << 16))
        fcntl.ioctl(qfd, IOWR(Q, 8, len(r)), r); print('shared buffer set')
        print('query:', query(qfd, name))
        mdt, data, arch = image(name)
        size = (len(data) + 4095) & ~4095
        ifd, im, _ = ion_alloc(ion, heap, size); im[:len(data)] = data
        req = bytearray(struct.pack('IIi64sII', len(mdt), len(data), ifd, name.encode(), arch, 0))
        try:
            fcntl.ioctl(qfd, IOWR(Q, 7, len(req)), req)
            print('LOAD OK app_id', struct.unpack('IIi64sII', req)[5], 'arch', arch)
        except OSError as e:
            sys.exit('LOAD FAILED %s' % errno.errorcode.get(e.errno))
        if len(sys.argv) > 3 and sys.argv[3] == 'buildinfo':
            print('build info:', fpc_cmd(qfd, ion, sbm, sbva, 12, 1, 0x414))
            sys.argv = sys.argv[:3]
        for hx in sys.argv[3:]:
            c = bytes.fromhex(hx); sbm[:len(c)] = c; sbm[1024:2048] = b'\0' * 1024
            s = bytearray(struct.pack('QI4xQI4x', sbva, len(c), sbva + 1024, 1024))
            try:
                fcntl.ioctl(qfd, IOWR(Q, 3, len(s)), s)
                print('cmd', hx, '->', sbm[1024:1024 + 64].hex())
            except OSError as e:
                print('cmd', hx, 'error', errno.errorcode.get(e.errno))
        fcntl.ioctl(qfd, IO(Q, 9)); print('unloaded')
