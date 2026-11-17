"""Bounded first-party PE directory access and append-only section relocation.

Public helpers never load/execute a module. append_data supports PE32/PE32+,
including header growth; RVAs/code stay unchanged. Signed images, unknown file
pointer layouts and nonzero overlays are explicit errors, not stripped data.
"""
import struct
from unity_clr import require, take, unpack, align


class PE:
    def __init__(self, raw):
        self.raw = raw
        require(take(raw, 0, 2) == b'MZ', 'PE_DOS_SIGNATURE')
        self.pe = unpack('<I', raw, 60)[0]
        require(take(raw, self.pe, 4) == b'PE\0\0', 'PE_SIGNATURE')
        self.count = unpack('<H', raw, self.pe + 6)[0]
        optsize = unpack('<H', raw, self.pe + 20)[0]
        self.opt = self.pe + 24
        magic = unpack('<H', raw, self.opt)[0]
        require(magic in (0x10b, 0x20b), 'PE_OPTIONAL_HEADER')
        dd_offset = 96 if magic == 0x10b else 112
        require(optsize >= dd_offset, 'PE_OPTIONAL_HEADER')
        self.dd = self.opt + dd_offset
        self.directory_count = unpack('<I', raw, self.dd - 4)[0]
        require(dd_offset + self.directory_count * 8 <= optsize, 'PE_DIRECTORY_BOUNDS')
        self.section_alignment, self.file_alignment = unpack('<II', raw, self.opt + 32)
        self.header_size = unpack('<I', raw, self.opt + 60)[0]
        self.section_table = self.opt + optsize
        take(raw, self.section_table, self.count * 40)
        require(self.section_table + self.count * 40 <= self.header_size <= len(raw), 'PE_HEADER_BOUNDS')
        self.sections = []
        for index in range(self.count):
            header = self.section_table + index * 40
            vs, va, rs, rp = unpack('<IIII', raw, header + 8)
            take(raw, rp, rs)
            require(not rs or rp >= self.header_size, 'PE_SECTION_HEADER_OVERLAP')
            self.sections.append({'header': header, 'virtual_size': vs, 'rva': va, 'size': rs, 'offset': rp})
        for index, a in enumerate(self.sections):
            for b in self.sections[:index]:
                require(not (a['offset'] < b['offset'] + b['size'] and b['offset'] < a['offset'] + a['size']), 'PE_SECTION_OVERLAP')
                require(not (a['rva'] < b['rva'] + max(b['size'], b['virtual_size']) and b['rva'] < a['rva'] + max(a['size'], a['virtual_size'])), 'PE_VIRTUAL_OVERLAP')

    def at(self, rva, size=1):
        if 0 <= rva and rva + size <= self.header_size:
            return rva
        matches = [s for s in self.sections if s['rva'] <= rva and rva - s['rva'] + size <= s['size']]
        require(len(matches) == 1, 'PE_RVA_BOUNDS')
        return matches[0]['offset'] + rva - matches[0]['rva']

    def directory(self, index):
        require(type(index) is int and 0 <= index < self.directory_count, 'PE_DIRECTORY_INDEX')
        return unpack('<II', self.raw, self.dd + index * 8)


def directory_info(raw, index):
    return PE(raw).directory(index)


def read_rva(raw, rva, size):
    pe = PE(raw)
    return take(raw, pe.at(rva, size), size)


def read_directory(raw, index):
    pe = PE(raw)
    rva, size = pe.directory(index)
    if not rva and not size:
        return b''
    require(index != 4, 'PE_CERTIFICATE_NOT_RVA')
    return take(raw, pe.at(rva, size), size)


def _checksum(raw, offset):
    total = 0
    for p in range(0, len(raw), 2):
        total += 0 if offset <= p < offset + 4 else int.from_bytes(raw[p:p + 2], 'little')
        total = (total & 65535) + (total >> 16)
    total = (total & 65535) + (total >> 16)
    return (total & 65535) + len(raw)


def _unsigned(pe):
    if pe.directory_count > 4:
        require(not any(pe.directory(4)), 'PE_PATCH_AUTHENTICODE')
    require(not unpack('<H', pe.raw, pe.opt + 70)[0] & 0x80, 'PE_PATCH_FORCE_INTEGRITY')
    if pe.directory_count > 14:
        rva, size = pe.directory(14)
        if rva:
            require(size >= 72, 'CLR_HEADER_SIZE')
            cli = pe.at(rva, size)
            require(not unpack('<I', pe.raw, cli + 16)[0] & 8 and not any(unpack('<II', pe.raw, cli + 32)), 'PE_PATCH_STRONG_NAME')


def set_directory(raw, index, rva, size):
    pe = PE(raw)
    _unsigned(pe)
    pe.directory(index)
    require(index != 4 and 0 <= rva <= 0xffffffff and 0 <= size <= 0xffffffff, 'PE_DIRECTORY_VALUE')
    if size:
        pe.at(rva, size)
    result = bytearray(raw)
    struct.pack_into('<II', result, pe.dd + index * 8, rva, size)
    struct.pack_into('<I', result, pe.opt + 64, _checksum(result, pe.opt + 64))
    return bytes(result)


def append_data(raw, payload):
    """Return (rebuilt_bytes,new_rva), adding one read-only data section.

    If section headers need more room, insert aligned header padding and update
    section raw pointers and debug-directory file pointers. No RVAs change.
    COFF symbols/section relocations/line tables are rejected as unsafe layouts.
    """
    require(isinstance(raw, bytes) and isinstance(payload, bytes) and payload, 'PE_APPEND_ARGUMENTS')
    pe = PE(raw)
    _unsigned(pe)
    require(pe.sections and pe.count < 96, 'PE_PATCH_SECTION_COUNT')
    require(0x200 <= pe.file_alignment <= 0x10000 and pe.section_alignment >= pe.file_alignment, 'PE_PATCH_ALIGNMENT')
    align(0, pe.file_alignment); align(0, pe.section_alignment)
    require(all(s['offset'] % pe.file_alignment == 0 and s['rva'] % pe.section_alignment == 0 for s in pe.sections), 'PE_PATCH_ALIGNMENT')
    require(not any(unpack('<II', raw, pe.pe + 12)), 'PE_PATCH_COFF_SYMBOLS_UNSUPPORTED')
    for section in pe.sections:
        require(not any(unpack('<IIHH', raw, section['header'] + 24)), 'PE_PATCH_SECTION_FILE_POINTERS_UNSUPPORTED')
    end = max(s['offset'] + s['size'] for s in pe.sections)
    require(not any(raw[end:]), 'PE_PATCH_OVERLAY')
    new_header = pe.section_table + pe.count * 40
    require(not any(take(raw, new_header, min(40, pe.header_size - new_header))), 'PE_PATCH_HEADER_OCCUPIED')
    new_header_size = align(max(pe.header_size, new_header + 40), pe.file_alignment)
    require(new_header_size <= min(s['rva'] for s in pe.sections), 'PE_PATCH_HEADER_VIRTUAL_OVERLAP')
    growth = new_header_size - pe.header_size
    result = bytearray(raw[:pe.header_size] + bytes(growth) + raw[pe.header_size:])
    if growth:
        for section in pe.sections:
            if section['offset']:
                struct.pack_into('<I', result, section['header'] + 20, section['offset'] + growth)
        struct.pack_into('<I', result, pe.opt + 60, new_header_size)
        if pe.directory_count > 6:
            debug_rva, debug_size = pe.directory(6)
            if debug_rva:
                require(debug_size % 28 == 0, 'PE_PATCH_DEBUG_DIRECTORY')
                debug = pe.at(debug_rva, debug_size)
                require(debug >= pe.header_size, 'PE_PATCH_DEBUG_IN_HEADERS')
                for pos in range(debug + growth, debug + growth + debug_size, 28):
                    size, pointer = unpack('<I', result, pos + 16)[0], unpack('<I', result, pos + 24)[0]
                    if pointer:
                        take(raw, pointer, size)
                        require(pointer >= pe.header_size, 'PE_PATCH_DEBUG_POINTER')
                        struct.pack_into('<I', result, pos + 24, pointer + growth)
    raw_offset = align(len(result), pe.file_alignment)
    raw_size = align(len(payload), pe.file_alignment)
    rva = align(max(s['rva'] + max(s['size'], s['virtual_size']) for s in pe.sections), pe.section_alignment)
    require(rva + raw_size <= 0xffffffff and raw_offset + raw_size <= 0xffffffff, 'PE_PATCH_IMAGE_LIMIT')
    result.extend(bytes(raw_offset - len(result)) + payload + bytes(raw_size - len(payload)))
    struct.pack_into('<8sIIIIIIHHI', result, new_header, b'.p0tdata', len(payload), rva, raw_size, raw_offset, 0, 0, 0, 0, 0x40000040)
    struct.pack_into('<H', result, pe.pe + 6, pe.count + 1)
    struct.pack_into('<I', result, pe.opt + 56, align(rva + len(payload), pe.section_alignment))
    initialized = unpack('<I', raw, pe.opt + 8)[0]
    require(initialized + raw_size <= 0xffffffff, 'PE_PATCH_IMAGE_LIMIT')
    struct.pack_into('<I', result, pe.opt + 8, initialized + raw_size)
    struct.pack_into('<I', result, pe.opt + 64, _checksum(result, pe.opt + 64))
    return bytes(result), rva
