"""USB links to a Jieli update-mode controller.

In update mode the controller is a USB mass-storage device that takes vendor SCSI commands.
macOS has no SCSI pass-through for it, so BulkOnlyLink drives the bulk endpoints through the
IOUSBHost helper (native/jl_usb.m) and frames Bulk-Only CBW/CSW itself. Windows and Linux hand
the CDB to the kernel (SCSI_PASS_THROUGH_DIRECT on \\\\.\\PhysicalDriveN, SG_IO on /dev/sgN)
through jl-uboot-tool's scsiio, and the OS does the framing. Both report the same data and
residue to the device transports, whose allowlists run before anything reaches a link.
"""
import ctypes
import struct
import sys
from pathlib import Path

from ...paths import resource

INQUIRY = bytes.fromhex('120000002400')
CBW = '<4sIIBBB16s'
CSW = '<4sIIB'


class LinkError(RuntimeError):
    pass


class AmbiguousTarget(LinkError):
    pass


class Link:
    """Bookkeeping shared by both links. Chip profiles override the residue and reply hooks."""

    transport = 'none'

    def __init__(self):
        self.tag = 0
        self.opened = False
        self.last_command = None
        self.last_data = None

    def expected_residue(self, cdb, out, n):
        return 0

    def residue_policy(self, cdb, expected):
        return 'standard-zero'

    def accept(self, cdb, data, expected):
        pass

    def begin(self, cdb, out, n):
        cdb = bytes(cdb)
        expected = self.expected_residue(cdb, out, n)
        self.tag += 1
        self.last_data = None
        size = len(out) if out is not None else n
        self.last_command = dict(cdb=cdb.hex(), tag=self.tag, transport=self.transport,
                                 direction='out' if out is not None else 'in', size=size,
                                 stage='command', complete=False, expected_residue=expected,
                                 residue_policy=self.residue_policy(cdb, expected))
        return cdb, expected, size


class BulkOnlyLink(Link):
    """macOS: Bulk-Only Transport over the IOUSBHost helper, opened by VID:PID (exactly one device)."""

    transport = 'macos-iousbhost-bulk-only'

    def __init__(self, native, vendor, product):
        super().__init__()
        self.native = native
        self.vendor = vendor
        self.product = product

    @staticmethod
    def library():
        native = ctypes.CDLL(str(resource('native/libjl_usb.dylib')))
        native.jl_error.restype = ctypes.c_char_p
        native.jl_open.argtypes = [ctypes.c_int, ctypes.c_int]
        native.jl_transfer.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        return native

    @classmethod
    def load(cls, vendor, product):
        return cls(cls.library(), vendor, product)

    def try_open(self):
        code = self.native.jl_open(self.vendor, self.product)
        if code == 0:
            self.opened = True
            return True
        if code == -2:
            return False
        message = self.native.jl_error().decode(errors='replace')
        if code == -3:
            raise AmbiguousTarget(message)
        if 'e00002c1' in message.lower() or 'not privileged' in message.lower():
            raise LinkError('macOS refused USB access; the flasher needs administrator rights for this controller. ' + message)
        raise LinkError(message)

    def close(self):
        if self.opened:
            self.native.jl_close()
            self.opened = False

    def transfer(self, ep, data_or_size):
        if ep == 1:
            data = bytes(data_or_size)
            buf = ctypes.create_string_buffer(data)
            size = len(data)
        else:
            size = data_or_size
            buf = ctypes.create_string_buffer(size)
        count = self.native.jl_transfer(ep, buf, size, 10000)
        if count < 0:
            raise LinkError(self.native.jl_error().decode(errors='replace'))
        if count != size:
            raise LinkError(f'Short USB transfer: {count}/{size}')
        return buf.raw[:count]

    def execute(self, cdb, out=None, n=0):
        cdb, expected, size = self.begin(cdb, out, n)
        cbw = struct.pack(CBW, b'USBC', self.tag, size, 0x80 if n else 0, 0, len(cdb), cdb.ljust(16, b'\0'))
        self.last_command['cbw'] = cbw.hex()
        self.transfer(1, cbw)
        data = b''
        self.last_command['stage'] = 'data'
        if out is not None:
            self.transfer(1, out)
        elif n:
            data = self.transfer(0x81, n)
            # Kept as evidence even if the status that follows is rejected; never returned then.
            self.last_data = data
        self.last_command['stage'] = 'status'
        csw = self.transfer(0x81, 13)
        signature, tag, residue, status = struct.unpack(CSW, csw)
        self.last_command.update(csw=csw.hex(), signature=signature.hex(), returned_tag=tag,
                                 residue=residue, status=status)
        if (signature, tag, residue, status) != (b'USBS', self.tag, expected, 0):
            raise LinkError(f'Failed USB command status: cdb={cdb.hex()} csw={csw.hex()} '
                            f'tag={tag}/{self.tag} residue={residue}/{size} expected_residue={expected} status={status}')
        self.accept(cdb, data, expected)
        self.last_command.update(stage='complete', complete=True)
        return data


def scsi_device_class():
    """jl-uboot-tool's SCSIDev for this OS. On Windows every data-in byte is returned even when the
    driver reports a residue, so the transports' content checks (not a truncated copy) decide."""
    if sys.platform == 'win32':
        import ctypes as c
        from scsiio import win32port as w

        class FullBufferSCSIDev(w.SCSIDev):
            def execute(self, cdb, data_out, data_in, max_sense_len=32, return_sense_buffer=False):
                sptd = w.SCSI_PASS_THROUGH_DIRECT()
                sptd.len = c.sizeof(sptd)
                sptd.path_id, sptd.target_id, sptd.lun = 0, 1, 0
                sptd.timeout = 10
                sptd.cdb_len = len(cdb)
                for i in range(sptd.cdb_len):
                    sptd.cdb[i] = cdb[i]
                if data_out and data_in:
                    raise NotImplementedError('Bidirectional transfers are not supported')
                if data_out:
                    sptd.dir, sptd.data_len = w.SCSI_IOCTL_DATA_OUT, len(data_out)
                    databuff = c.create_string_buffer(bytes(data_out), len(data_out))
                elif data_in:
                    sptd.dir, sptd.data_len = w.SCSI_IOCTL_DATA_IN, len(data_in)
                    databuff = c.create_string_buffer(len(data_in))
                else:
                    sptd.dir, databuff = w.SCSI_IOCTL_DATA_UNSPECIFIED, None
                if databuff is not None:
                    sptd.data_ptr = c.cast(databuff, c.POINTER(w.BYTE))
                requested = sptd.data_len
                returned = w.DWORD()
                pointer = c.cast(c.addressof(sptd), c.POINTER(w.BYTE))
                if not w.DeviceIoControl(self.fhandle, w.IOCTL_SCSI_PASS_THROUGH_DIRECT, pointer, sptd.len,
                                         c.byref(sptd), sptd.len, c.byref(returned), 0):
                    raise w.SCSIException('Transfer failed: %s' % c.WinError())
                if sptd.status:
                    raise w.SCSIException('SCSI status %#x' % sptd.status)
                if data_in:
                    data_in[:] = bytearray(databuff.raw[:len(data_in)])
                return requested - sptd.data_len

        return FullBufferSCSIDev
    if sys.platform.startswith('linux'):
        from scsiio.linuxport import SCSIDev
        return SCSIDev
    raise LinkError(f'SCSI pass-through is not available on {sys.platform}')


def candidate_paths(platform=None):
    platform = platform or sys.platform
    if platform == 'win32':
        return [f'\\\\.\\PhysicalDrive{i}' for i in range(32)]
    if platform.startswith('linux'):
        return sorted(str(p) for p in Path('/dev').glob('sg*'))
    return []


class PassThroughLink(Link):
    """Windows and Linux: the kernel's SCSI pass-through on the update-mode disk, found by INQUIRY.

    Every candidate disk receives only an INQUIRY; exactly one must match `identity`."""

    transport = 'scsi-pass-through'

    def __init__(self, identity, dev_class=None, candidates=None, platform=None):
        super().__init__()
        self.identity = identity
        self.dev_class = dev_class
        self.candidates = candidates
        self.platform = platform or sys.platform
        self.dev = None
        self.path = None

    def try_open(self):
        dev_class = self.dev_class or scsi_device_class()
        found = []
        for path in (self.candidates if self.candidates is not None else candidate_paths(self.platform)):
            dev = dev_class()
            try:
                dev.open(path)
            except Exception:
                continue
            matched = False
            try:
                inquiry = bytearray(36)
                dev.execute(INQUIRY, None, inquiry)
                matched = bool(self.identity(bytes(inquiry)))
            except Exception:
                pass
            if matched:
                found.append((path, dev))
            else:
                dev.close()
        if len(found) > 1:
            for _, dev in found:
                dev.close()
            raise AmbiguousTarget('More than one controller is connected in update mode')
        if not found:
            return False
        self.path, self.dev = found[0]
        self.opened = True
        return True

    def close(self):
        if self.dev is not None:
            self.dev.close()
            self.dev = None
        self.opened = False

    def accepted_residues(self, expected):
        # Linux reports the device's CSW residue unchanged. Windows' USB storage driver may or may not
        # pass it through, so a nonzero expected residue may surface as zero there.
        if expected and self.platform == 'win32':
            return {expected, 0}
        return {expected}

    def execute(self, cdb, out=None, n=0):
        if self.dev is None:
            raise LinkError('Device not open')
        cdb, expected, size = self.begin(cdb, out, n)
        try:
            if out is not None:
                residue = self.dev.execute(cdb, bytes(out), None)
                data = b''
            elif n:
                buffer = bytearray(n)
                residue = self.dev.execute(cdb, None, buffer)
                data = bytes(buffer)
                self.last_data = data
            else:
                residue = self.dev.execute(cdb, None, None)
                data = b''
        except Exception as e:
            raise LinkError(f'SCSI command failed: cdb={cdb.hex()}: {e}') from e
        self.last_command.update(residue=residue, status=0)
        if residue not in self.accepted_residues(expected):
            raise LinkError(f'Failed SCSI command residue: cdb={cdb.hex()} residue={residue}/{size} expected_residue={expected}')
        self.accept(cdb, data, expected)
        self.last_command.update(stage='complete', complete=True)
        return data
