#!/usr/bin/env python3
"""Pure-stdlib JPEG luminance stats for an exposure aid (no PIL/numpy needed).

Decodes only the DC coefficient of each 8x8 luma block -> a 1/8-res brightness
map, enough to flag under/over exposure. To stay fast on 10 MP camera JPEGs it
meters off the embedded EXIF thumbnail when present (tiny), else decodes the
given image. Baseline (SOF0) Huffman JPEGs only; returns None on anything it
can't handle so callers degrade gracefully (capture never breaks).
"""

from __future__ import annotations

import struct


# ---- EXIF thumbnail extraction (fast path) --------------------------------

def _find_thumbnail(data: bytes) -> bytes | None:
    """Return the embedded EXIF (IFD1) thumbnail JPEG bytes, if any."""
    # locate APP1 "Exif\0\0"
    i = 2
    n = len(data)
    tiff = None
    while i + 4 <= n and data[i] == 0xFF:
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            tiff = data[i + 10:i + 2 + seglen]
            break
        if marker == 0xDA:
            break
        i += 2 + seglen
    if not tiff or len(tiff) < 8:
        return None
    bo = "<" if tiff[:2] == b"II" else ">" if tiff[:2] == b"MM" else None
    if bo is None:
        return None
    try:
        ifd0_off = struct.unpack_from(bo + "I", tiff, 4)[0]
        cnt = struct.unpack_from(bo + "H", tiff, ifd0_off)[0]
        ifd1_off = struct.unpack_from(bo + "I", tiff, ifd0_off + 2 + cnt * 12)[0]
        if ifd1_off == 0:
            return None
        cnt1 = struct.unpack_from(bo + "H", tiff, ifd1_off)[0]
        toff = tlen = None
        for k in range(cnt1):
            tag, _typ, _cnt, val = struct.unpack_from(
                bo + "HHII", tiff, ifd1_off + 2 + k * 12)
            if tag == 0x0201:
                toff = val
            elif tag == 0x0202:
                tlen = val
        if toff and tlen and tiff[toff:toff + 2] == b"\xff\xd8":
            return tiff[toff:toff + tlen]
    except (struct.error, IndexError):
        return None
    return None


# ---- minimal baseline-JPEG DC decoder -------------------------------------

def _build_huff(counts, symbols):
    table, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            table[(length, code)] = symbols[k]
            k += 1
            code += 1
        code <<= 1
    return table


class _Bits:
    """Bit reader over one restart interval (FF00 stuffing already removed)."""

    def __init__(self, chunk: bytes):
        self.d = chunk.replace(b"\xff\x00", b"\xff")
        self.i = 0
        self.buf = 0
        self.n = 0

    def bit(self) -> int:
        if self.n == 0:
            self.buf = self.d[self.i] if self.i < len(self.d) else 0
            self.i += 1
            self.n = 8
        self.n -= 1
        return (self.buf >> self.n) & 1

    def receive(self, s: int) -> int:
        v = 0
        for _ in range(s):
            v = (v << 1) | self.bit()
        return v

    def decode(self, table) -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.bit()
            sym = table.get((length, code))
            if sym is not None:
                return sym
        return 0


def _extend(v, s):
    return v - (1 << s) + 1 if s and v < (1 << (s - 1)) else v


def _luma_means(data: bytes):
    """Decode DC of the Y component -> list of block mean brightnesses (0-255)."""
    qt, huff_dc, huff_ac = {}, {}, {}
    comps = []          # (id, h, v, quant_id)
    scan = {}           # comp_id -> (dc_table, ac_table)
    width = height = 0
    restart = 0
    i, n = 2, len(data)
    entropy_start = None
    while i + 2 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xD9:
            break
        if i + 4 > n:
            break
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i + 4:i + 2 + seglen]
        if m == 0xDB:                                   # DQT
            p = 0
            while p < len(seg):
                pq, tq = seg[p] >> 4, seg[p] & 0xF
                p += 1
                if pq == 0:
                    qt[tq] = list(seg[p:p + 64])
                    p += 64
                else:
                    qt[tq] = [int.from_bytes(seg[p + 2 * j:p + 2 * j + 2], "big")
                              for j in range(64)]
                    p += 128
        elif m in (0xC0, 0xC1):                          # SOF0/1 baseline-ish
            height = int.from_bytes(seg[1:3], "big")
            width = int.from_bytes(seg[3:5], "big")
            nc = seg[5]
            for c in range(nc):
                cid = seg[6 + c * 3]
                samp = seg[7 + c * 3]
                comps.append((cid, samp >> 4, samp & 0xF, seg[8 + c * 3]))
        elif m in (0xC2, 0xC3):                          # progressive / lossless
            return None
        elif m == 0xC4:                                  # DHT
            p = 0
            while p < len(seg):
                tc, th = seg[p] >> 4, seg[p] & 0xF
                p += 1
                counts = list(seg[p:p + 16])
                p += 16
                total = sum(counts)
                syms = list(seg[p:p + total])
                p += total
                (huff_dc if tc == 0 else huff_ac)[th] = _build_huff(counts, syms)
        elif m == 0xDD:                                  # DRI
            restart = int.from_bytes(seg[0:2], "big")
        elif m == 0xDA:                                  # SOS
            ns = seg[0]
            for c in range(ns):
                cs = seg[1 + c * 2]
                tt = seg[2 + c * 2]
                scan[cs] = (tt >> 4, tt & 0xF)
            entropy_start = i + 2 + seglen
            break
        i += 2 + seglen

    if entropy_start is None or not comps or width == 0:
        return None
    eoi = data.find(b"\xff\xd9", entropy_start)
    entropy = data[entropy_start:eoi if eoi != -1 else n]

    hmax = max(c[1] for c in comps)
    vmax = max(c[2] for c in comps)
    mcux = (width + 8 * hmax - 1) // (8 * hmax)
    mcuy = (height + 8 * vmax - 1) // (8 * vmax)
    total_mcu = mcux * mcuy
    ycid = comps[0][0]                                   # Y is first component

    # split entropy into restart intervals
    if restart:
        chunks, last = [], 0
        j = 0
        while j < len(entropy) - 1:
            if entropy[j] == 0xFF and 0xD0 <= entropy[j + 1] <= 0xD7:
                chunks.append(entropy[last:j])
                last = j + 2
                j += 2
                continue
            j += 1
        chunks.append(entropy[last:])
    else:
        chunks = [entropy]

    means = []
    try:
        mcu_done = 0
        for chunk in chunks:
            if mcu_done >= total_mcu:
                break
            br = _Bits(chunk)
            pred = {c[0]: 0 for c in comps}
            count = restart if restart else total_mcu
            for _ in range(min(count, total_mcu - mcu_done)):
                for (cid, h, v, qid) in comps:
                    dct, act = scan.get(cid, (0, 0))
                    for _b in range(h * v):
                        s = br.decode(huff_dc[dct])
                        diff = _extend(br.receive(s), s) if s else 0
                        pred[cid] += diff
                        if cid == ycid:
                            q0 = qt.get(qid, [1])[0] or 1
                            mean = (pred[cid] * q0) / 8.0 + 128.0
                            means.append(0.0 if mean < 0 else 255.0 if mean > 255 else mean)
                        # consume AC coefficients (values discarded)
                        k = 1
                        while k < 64:
                            rs = br.decode(huff_ac[act])
                            r, ssize = rs >> 4, rs & 0xF
                            if ssize == 0:
                                if r != 15:
                                    break
                                k += 16
                            else:
                                br.receive(ssize)
                                k += r + 1
                mcu_done += 1
    except (KeyError, IndexError):
        return means or None
    return means or None


# ---- public API -----------------------------------------------------------

def _classify(mean, under, over):
    if over > 0.04:
        return "over", "Overexposed — highlights clipping. Use a faster shutter."
    if mean < 40 or under > 0.7:
        return "under", "Too dark — use a longer shutter."
    if mean > 210:
        return "bright", "A bit bright — try a slightly faster shutter."
    if mean < 60:
        return "dark", "A bit dark — try a slightly longer shutter."
    return "ok", "Looks well exposed."


def luma_stats(path) -> dict | None:
    """Return {'mean','under','over','status','advice'} or None if undecodable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    means = None
    try:
        thumb = _find_thumbnail(data)
        if thumb:
            means = _luma_means(thumb)
        if not means:                   # no thumb, or thumb undecodable
            means = _luma_means(data)
    except Exception:                   # never let the exposure aid break capture
        return None
    if not means:
        return None
    mean = sum(means) / len(means)
    under = sum(1 for x in means if x < 20) / len(means)
    over = sum(1 for x in means if x > 240) / len(means)
    status, advice = _classify(mean, under, over)
    return {"mean": round(mean, 1), "under": round(under, 3),
            "over": round(over, 3), "status": status, "advice": advice}


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(p, "->", luma_stats(p))
