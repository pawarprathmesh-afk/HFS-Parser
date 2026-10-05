"""
HFS+ transparent compression (the com.apple.decmpfs extended attribute).

A compressed file has an empty data fork (and the UF_COMPRESSED flag); its content lives in
a 16-byte decmpfs header attribute, followed by either the compressed bytes (inline types) or
nothing, with the compressed bytes in the resource fork (resource-fork types).

    header: "fpmc" | type (u32 LE) | uncompressed size (u64 LE) | [inline payload]

    type  3 zlib, inline          type  4 zlib, resource fork
    type  7 LZVN, inline          type  8 LZVN, resource fork
    type 11 LZFSE, inline         type 12 LZFSE, resource fork
    type  1 stored, inline

Resource-fork layouts (verified against volumes written by macOS and afsctool):
    zlib   : 0x100-byte header, then at 0x104 a LE u32 block count and (offset, size) LE pairs,
             offsets relative to 0x104.
    LZVN / LZFSE : LE u32 offset table at the start of the fork; block i is
             fork[off[i]:off[i+1]].
Each block expands to 65536 bytes (the last one to the remainder).

Pure-Python decoders for LZVN and LZFSE, ported from Apple's reference implementation
(github.com/lzfse/lzfse, BSD licensed), so no native library is needed.
"""

import struct
import zlib
from typing import Callable

CHUNK = 65536
MAGIC = b"fpmc"
TYPE_STORED_ATTR = 1
TYPE_ZLIB_ATTR, TYPE_ZLIB_RSRC = 3, 4
TYPE_LZVN_ATTR, TYPE_LZVN_RSRC = 7, 8
TYPE_LZFSE_ATTR, TYPE_LZFSE_RSRC = 11, 12


class DecmpfsError(Exception):
    pass


def _clean_errors(fn):
    """Corrupt input must surface as DecmpfsError, never IndexError/struct.error/zlib.error."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DecmpfsError:
            raise
        except (IndexError, struct.error, zlib.error, ValueError, OverflowError, KeyError) as e:
            raise DecmpfsError(f"corrupt compressed data ({type(e).__name__}: {e})") from None
    return wrapper


@_clean_errors
def parse_header(header: bytes):
    if len(header) < 16 or header[:4] != MAGIC:
        raise DecmpfsError("not a decmpfs header")
    ctype, size = struct.unpack("<IQ", header[4:16])
    return ctype, size


@_clean_errors
def uncompressed_size(header: bytes) -> int:
    return parse_header(header)[1]


# ---------------------------------------------------------------------------
# LZVN
# ---------------------------------------------------------------------------

# Opcode class for each of the 256 first bytes, generated from the reference decoder's jump table:
# d=small distance  D=medium distance  L=large distance  p=previous distance
# s=small literal   S=large literal    m=small match     M=large match
# e=end of stream   n=no-op            u=undefined
_LZVN_OPS = "ddddddeLddddddnLddddddnLdddddduLdddddduLdddddduLdddddduLdddddduLddddddpLddddddpLddddddpLddddddpLddddddpLddddddpLuuuuuuuuuuuuuuuuddddddpLddddddpLddddddpLddddddpLDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDddddddpLddddddpLuuuuuuuuuuuuuuuuSsssssssssssssssMmmmmmmmmmmmmmmm"


def _copy_match(out: bytearray, D: int, M: int):
    if D <= 0 or D > len(out):
        raise DecmpfsError("LZVN: invalid match distance")
    if D >= M:
        out += out[-D:len(out) - D + M]
    else:                       # overlapping copy repeats the last D bytes
        unit = bytes(out[-D:])
        out += (unit * (M // D + 1))[:M]


@_clean_errors
def lzvn_decode(src: bytes, dst_size: int) -> bytes:
    out = bytearray()
    i, n = 0, len(src)
    D = 0
    while i < n and len(out) < dst_size:
        opc = src[i]
        kind = _LZVN_OPS[opc]
        if kind == "e":
            break
        if kind == "n":
            i += 1
            continue
        if kind == "u":
            raise DecmpfsError(f"LZVN: undefined opcode 0x{opc:02x} at {i}")
        if kind in "dDLp":
            L = opc >> 6
            M = ((opc >> 3) & 7) + 3
            if kind == "d":
                opclen = 2
                if i + opclen > n:
                    break
                D = ((opc & 7) << 8) | src[i + 1]
            elif kind == "D":
                opclen = 3
                if i + opclen > n:
                    break
                L = (opc >> 3) & 3
                v = src[i + 1] | (src[i + 2] << 8)
                M = (((opc & 7) << 2) | (v & 3)) + 3
                D = v >> 2
            elif kind == "L":
                opclen = 3
                if i + opclen > n:
                    break
                D = src[i + 1] | (src[i + 2] << 8)
            else:           # p: reuse previous distance
                opclen = 1
            i += opclen
            out += src[i:i + L]
            i += L
            _copy_match(out, D, M)
        elif kind == "m":       # small match, previous distance
            _copy_match(out, D, opc & 15)
            i += 1
        elif kind == "M":       # large match, previous distance
            if i + 2 > n:
                break
            _copy_match(out, D, src[i + 1] + 16)
            i += 2
        elif kind == "s":       # small literal
            L = opc & 15
            i += 1
            out += src[i:i + L]
            i += L
        else:                   # S: large literal
            if i + 2 > n:
                break
            L = src[i + 1] + 16
            i += 2
            out += src[i:i + L]
            i += L
    return bytes(out[:dst_size])


# ---------------------------------------------------------------------------
# LZFSE
# ---------------------------------------------------------------------------

_L_SYMBOLS, _M_SYMBOLS, _D_SYMBOLS, _LIT_SYMBOLS = 20, 20, 64, 256
_L_STATES, _M_STATES, _D_STATES, _LIT_STATES = 64, 64, 256, 1024
_L_EXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 3, 5, 8)
_L_BASE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 28, 60)
_M_EXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 5, 8, 11)
_M_BASE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 24, 56, 312)
_D_EXTRA = tuple(v for v in range(16) for _ in range(4))
_D_BASE = (0, 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 28, 36, 44, 52, 60, 76, 92, 108,
           124, 156, 188, 220, 252, 316, 380, 444, 508, 636, 764, 892, 1020, 1276, 1532,
           1788, 2044, 2556, 3068, 3580, 4092, 5116, 6140, 7164, 8188, 10236, 12284,
           14332, 16380, 20476, 24572, 28668, 32764, 40956, 49148, 57340, 65532, 81916,
           98300, 114684, 131068, 163836, 196604, 229372)

_FREQ_NBITS = (2, 3, 2, 5, 2, 3, 2, 8, 2, 3, 2, 5, 2, 3, 2, 14,
               2, 3, 2, 5, 2, 3, 2, 8, 2, 3, 2, 5, 2, 3, 2, 14)
_FREQ_VALUE = (0, 2, 1, 4, 0, 3, 1, -1, 0, 2, 1, 5, 0, 3, 1, -1,
               0, 2, 1, 6, 0, 3, 1, -1, 0, 2, 1, 7, 0, 3, 1, -1)


def _clz32(x: int) -> int:
    return 32 - x.bit_length()


def _init_decoder_table(nstates, freq):
    """Plain FSE decoder table: entries (k, symbol, delta)."""
    n_clz = _clz32(nstates)
    table, total = [], 0
    for sym, f in enumerate(freq):
        if f == 0:
            continue
        total += f
        if total > nstates:
            raise DecmpfsError("LZFSE: frequency table overflow")
        k = _clz32(f) - n_clz
        j0 = ((2 * nstates) >> k) - f
        for j in range(f):
            if j < j0:
                table.append((k, sym, ((f + j) << k) - nstates))
            else:
                table.append((k - 1, sym, (j - j0) << (k - 1)))
    return table


def _init_value_decoder_table(nstates, freq, vbits, vbase):
    """Value FSE decoder table: entries (total_bits, value_bits, delta, vbase)."""
    n_clz = _clz32(nstates)
    table, total = [], 0
    for sym, f in enumerate(freq):
        if f == 0:
            continue
        total += f
        if total > nstates:
            raise DecmpfsError("LZFSE: frequency table overflow")
        k = _clz32(f) - n_clz
        j0 = ((2 * nstates) >> k) - f
        for j in range(f):
            if j < j0:
                table.append((k + vbits[sym], vbits[sym], ((f + j) << k) - nstates, vbase[sym]))
            else:
                table.append((k - 1 + vbits[sym], vbits[sym], (j - j0) << (k - 1), vbase[sym]))
    return table


class _BitStream:
    """FSE input stream: 64-bit accumulator refilled backwards from the end of a buffer."""

    def __init__(self, data: bytes, pos: int, start: int, nbits: int):
        self.data, self.pos, self.start = data, pos, start
        if nbits:
            if pos < start + 8:
                raise DecmpfsError("LZFSE: stream out of range")
            self.pos -= 8
            self.accum = int.from_bytes(data[self.pos:self.pos + 8], "little")
            self.nbits = nbits + 64
        else:
            if pos < start + 7:
                raise DecmpfsError("LZFSE: stream out of range")
            self.pos -= 7
            self.accum = int.from_bytes(data[self.pos:self.pos + 7], "little")
            self.nbits = 56
        if self.nbits < 56 or self.nbits >= 64 or (self.accum >> self.nbits) != 0:
            raise DecmpfsError("LZFSE: invalid stream initial state")

    def flush(self):
        n = (63 - self.nbits) & -8
        pos = self.pos - (n >> 3)
        if pos < self.start:
            raise DecmpfsError("LZFSE: stream underrun")
        self.pos = pos
        incoming = int.from_bytes(self.data[pos:pos + 8], "little")
        self.accum = (self.accum << n) | (incoming & ((1 << n) - 1))
        self.nbits += n

    def pull(self, n: int) -> int:
        self.nbits -= n
        result = self.accum >> self.nbits
        self.accum &= (1 << self.nbits) - 1
        return result


def _decode_freq_tables(buf: bytes, pos: int, end: int):
    """Decode the compressed (v2) frequency tables: l, m, d and literal frequencies, concatenated."""
    total = _L_SYMBOLS + _M_SYMBOLS + _D_SYMBOLS + _LIT_SYMBOLS
    if pos == end:
        return [0] * total              # tables omitted
    freqs = []
    accum, accum_nbits = 0, 0
    for _ in range(total):
        while pos < end and accum_nbits + 8 <= 32:
            accum |= buf[pos] << accum_nbits
            accum_nbits += 8
            pos += 1
        b = accum & 31
        nb = _FREQ_NBITS[b]
        if nb == 8:
            v = 8 + ((accum >> 4) & 0xF)
        elif nb == 14:
            v = 24 + ((accum >> 4) & 0x3FF)
        else:
            v = _FREQ_VALUE[b]
        if nb > accum_nbits:
            raise DecmpfsError("LZFSE: truncated frequency table")
        accum >>= nb
        accum_nbits -= nb
        freqs.append(v)
    if accum_nbits >= 8 or pos != end:
        raise DecmpfsError("LZFSE: bad frequency table length")
    return freqs


@_clean_errors
def lzfse_decode(src: bytes, dst_size: int = None) -> bytes:
    data = bytes(src) + b"\x00" * 16        # padding so 8-byte loads never run short
    n = len(src)
    out = bytearray()
    pos = 0
    while True:
        if pos + 4 > n:
            raise DecmpfsError("LZFSE: truncated stream")
        magic = data[pos:pos + 4]
        if magic == b"bvx$":                  # end of stream
            break
        if magic == b"bvx-":                  # stored block
            (nraw,) = struct.unpack("<I", data[pos + 4:pos + 8])
            out += data[pos + 8:pos + 8 + nraw]
            pos += 8 + nraw
        elif magic == b"bvxn":                # LZVN block
            nraw, npayload = struct.unpack("<II", data[pos + 4:pos + 12])
            out += lzvn_decode(data[pos + 12:pos + 12 + npayload], nraw)
            pos += 12 + npayload
        elif magic in (b"bvx1", b"bvx2"):
            pos = _lzfse_compressed_block(data, n, pos, magic, out)
        else:
            raise DecmpfsError(f"LZFSE: invalid block magic {magic!r}")
        if dst_size is not None and len(out) >= dst_size:
            break
    return bytes(out if dst_size is None else out[:dst_size])


def _lzfse_compressed_block(data: bytes, n: int, pos: int, magic: bytes, out: bytearray) -> int:
    """Decode one bvx1/bvx2 block, appending to `out`. Returns the position after the block."""
    base = pos
    if magic == b"bvx2":
        v0, v1, v2 = struct.unpack("<QQQ", data[pos + 8:pos + 32])
        header_size = v2 & 0xFFFFFFFF
        n_literals = v0 & 0xFFFFF
        n_lit_bytes = (v0 >> 20) & 0xFFFFF
        literal_bits = ((v0 >> 60) & 7) - 7
        lit_states = [(v1 >> (10 * i)) & 0x3FF for i in range(4)]
        n_matches = (v0 >> 40) & 0xFFFFF
        n_lmd_bytes = (v1 >> 40) & 0xFFFFF
        lmd_bits = ((v1 >> 60) & 7) - 7
        l_state, m_state, d_state = (v2 >> 32) & 0x3FF, (v2 >> 42) & 0x3FF, (v2 >> 52) & 0x3FF
        freqs = _decode_freq_tables(data, pos + 32, pos + header_size)
    else:
        (_nraw, _npay, n_literals, n_matches, n_lit_bytes, n_lmd_bytes) = \
            struct.unpack("<6I", data[pos + 4:pos + 28])
        literal_bits = struct.unpack("<i", data[pos + 28:pos + 32])[0]
        lit_states = list(struct.unpack("<4H", data[pos + 32:pos + 40]))
        lmd_bits = struct.unpack("<i", data[pos + 40:pos + 44])[0]
        l_state, m_state, d_state = struct.unpack("<3H", data[pos + 44:pos + 50])
        total = _L_SYMBOLS + _M_SYMBOLS + _D_SYMBOLS + _LIT_SYMBOLS
        freqs = list(struct.unpack("<%dH" % total, data[pos + 50:pos + 50 + 2 * total]))
        header_size = 772
    if pos + header_size + n_lit_bytes + n_lmd_bytes > n:
        raise DecmpfsError("LZFSE: truncated block")
    lf = freqs[:_L_SYMBOLS]
    mf = freqs[_L_SYMBOLS:_L_SYMBOLS + _M_SYMBOLS]
    df = freqs[_L_SYMBOLS + _M_SYMBOLS:_L_SYMBOLS + _M_SYMBOLS + _D_SYMBOLS]
    litf = freqs[_L_SYMBOLS + _M_SYMBOLS + _D_SYMBOLS:]
    lit_tab = _init_decoder_table(_LIT_STATES, litf)
    l_tab = _init_value_decoder_table(_L_STATES, lf, _L_EXTRA, _L_BASE)
    m_tab = _init_value_decoder_table(_M_STATES, mf, _M_EXTRA, _M_BASE)
    d_tab = _init_value_decoder_table(_D_STATES, df, _D_EXTRA, _D_BASE)

    # literals: four interleaved FSE streams, read backwards from the end of the literal payload
    lit_end = base + header_size + n_lit_bytes
    s = _BitStream(data, lit_end, 0, literal_bits)
    st = lit_states
    literals = bytearray(n_literals)
    for i in range(0, n_literals, 4):
        s.flush()
        for k in range(4):
            nb, sym, delta = lit_tab[st[k]]
            st[k] = delta + s.pull(nb)
            literals[i + k] = sym

    # literal-length / match-length / distance stream, read backwards from the end of the payload
    lmd_end = lit_end + n_lmd_bytes
    s = _BitStream(data, lmd_end, lit_end, lmd_bits)
    lp = 0
    D = -1
    for _ in range(n_matches):
        s.flush()
        tb, vb, delta, vbase = l_tab[l_state]
        v = s.pull(tb)
        l_state = delta + (v >> vb)
        L = vbase + (v & ((1 << vb) - 1))
        tb, vb, delta, vbase = m_tab[m_state]
        v = s.pull(tb)
        m_state = delta + (v >> vb)
        M = vbase + (v & ((1 << vb) - 1))
        tb, vb, delta, vbase = d_tab[d_state]
        v = s.pull(tb)
        d_state = delta + (v >> vb)
        newd = vbase + (v & ((1 << vb) - 1))
        if newd:
            D = newd
        if D < 0 or D > len(out) + L:
            raise DecmpfsError("LZFSE: invalid match distance")
        out += literals[lp:lp + L]
        lp += L
        if M:
            _copy_match(out, D, M)
    return lmd_end


# ---------------------------------------------------------------------------
# Container handling
# ---------------------------------------------------------------------------

def _inflate_chunk(kind: str, chunk: bytes, out_len: int) -> bytes:
    if kind == "zlib":
        if chunk and (chunk[0] & 0x0F) == 0x0F:        # stored marker
            return chunk[1:1 + out_len]
        return zlib.decompress(chunk)[:out_len]
    if kind == "lzvn":
        if chunk and chunk[0] == 0x06:                 # stored marker
            return chunk[1:1 + out_len]
        return lzvn_decode(chunk, out_len)
    if chunk[:3] == b"bvx":                            # lzfse
        return lzfse_decode(chunk, out_len)
    if chunk and chunk[0] == 0x06:
        return chunk[1:1 + out_len]
    raise DecmpfsError("unrecognised LZFSE chunk")


@_clean_errors
def decompress(header: bytes, read_resource_fork: Callable[[], bytes]) -> bytes:
    """header = full com.apple.decmpfs attribute value. read_resource_fork() is called only
    for resource-fork types."""
    ctype, size = parse_header(header)
    payload = header[16:]
    if ctype == TYPE_STORED_ATTR:
        return payload[:size]
    if ctype in (TYPE_ZLIB_ATTR, TYPE_LZVN_ATTR, TYPE_LZFSE_ATTR):
        kind = {TYPE_ZLIB_ATTR: "zlib", TYPE_LZVN_ATTR: "lzvn", TYPE_LZFSE_ATTR: "lzfse"}[ctype]
        return _inflate_chunk(kind, payload, size)
    if size == 0:
        return b""
    rsrc = read_resource_fork()
    nblocks = (size + CHUNK - 1) // CHUNK
    if ctype == TYPE_ZLIB_RSRC:
        base = 0x104
        (count,) = struct.unpack("<I", rsrc[base:base + 4])
        if count < nblocks:
            raise DecmpfsError("zlib resource fork: block count too small")
        spans = []
        for i in range(nblocks):
            off, length = struct.unpack("<II", rsrc[base + 4 + 8 * i:base + 12 + 8 * i])
            spans.append((base + off, base + off + length))
        kind = "zlib"
    elif ctype in (TYPE_LZVN_RSRC, TYPE_LZFSE_RSRC):
        offsets = struct.unpack("<%dI" % (nblocks + 1), rsrc[:4 * (nblocks + 1)])
        spans = [(offsets[i], offsets[i + 1]) for i in range(nblocks)]
        kind = "lzvn" if ctype == TYPE_LZVN_RSRC else "lzfse"
    else:
        raise DecmpfsError(f"unsupported decmpfs compression type {ctype}")
    out = bytearray()
    for i, (a, b) in enumerate(spans):
        out += _inflate_chunk(kind, rsrc[a:b], min(CHUNK, size - i * CHUNK))
    if len(out) != size:
        raise DecmpfsError(f"decompressed {len(out)} bytes, expected {size}")
    return bytes(out)


@_clean_errors
def decompress_resource_fork_only(rsrc: bytes) -> bytes:
    """Decompress a resource-fork-compressed file when the com.apple.decmpfs attribute (which
    names the codec and the exact size) is no longer available, e.g. a deleted file. The
    layout and codec are inferred from the fork itself; the result is exact for zlib/LZVN/LZFSE
    forks because each block is self-delimiting."""
    if len(rsrc) < 16:
        raise DecmpfsError("resource fork too small to be compressed data")
    if rsrc[:4] == b"\x00\x00\x01\x00" and len(rsrc) > 0x108:           # zlib layout
        base = 0x104
        (count,) = struct.unpack("<I", rsrc[base:base + 4])
        out = bytearray()
        for i in range(count):
            off, length = struct.unpack("<II", rsrc[base + 4 + 8 * i:base + 12 + 8 * i])
            out += _inflate_chunk("zlib", rsrc[base + off:base + off + length], CHUNK)
        return bytes(out)
    (first,) = struct.unpack("<I", rsrc[:4])
    if first < 8 or first % 4 or first > len(rsrc):
        raise DecmpfsError("resource fork is not in a known compressed layout")
    words = list(struct.unpack("<%dI" % (first // 4), rsrc[:first]))
    while words and words[-1] == 0:                                      # LZFSE tables carry a zero pad word
        words.pop()
    if any(b < a for a, b in zip(words, words[1:])) or words[-1] > len(rsrc):
        raise DecmpfsError("resource fork offset table is inconsistent")
    out = bytearray()
    for a, b in zip(words, words[1:]):
        chunk = rsrc[a:b]
        out += _inflate_chunk("lzfse" if chunk[:3] == b"bvx" else "lzvn", chunk, CHUNK)
    return bytes(out)
