"""Minimal MPEG-TS demultiplexer: enough of ISO/IEC 13818-1 to isolate
the H.264 elementary stream from a Blink stream, without going through PyAV.

PAT (PID 0) -> PMT PID -> PMT -> video PID (stream_type 0x1B = H.264)
-> reassembly into a continuous elementary stream (concatenation of PES
payloads on that PID, PES header skipped at each new PES packet, its
PTS retained along the way - see below) -> split into NAL units as soon as
their end is known (next start code seen), without waiting for a PES packet
or a whole frame to be complete - a key frame can weigh several
tens of KB, far more than a single TS packet (188 bytes), whereas
SPS/PPS fit in a handful of bytes right at the start.

The PTS of each PES packet (ITU-T H.222.0, 2.4.3.7) is extracted and associated
with the NAL units that come from it: an improvised timestamp at reception
(rather than the real one encoded by the camera at capture) makes playback
jerky as soon as the network delivers in bursts rather than at a steady rate
(observed in the wild, 2026-09-03) - the receiver then loses the timing
information it needs to smooth display. Convenience: the MPEG PTS
is already clocked at 90 kHz, the same clock WebRTC uses for
RTP video - no conversion needed, just read it correctly."""

from typing import Optional


class DemuxeurTSVideo:
    def __init__(self):
        self.pmt_pid = None
        self.video_pid = None
        self._flux_elementaire = bytearray()
        self._reste_ts = b""
        # [(position_in_elementary_stream, pts), ...], increasing position.
        self._marques_pts: list = []

    def alimenter(self, data: bytes) -> list:
        """Digests an arbitrary block (0, 1 or several 188-byte TS packets,
        potentially misaligned). Returns the newly available (pts_or_None,
        nal_bytes), in order - nal_bytes in Annex B, 4-byte start code
        included."""
        data = self._reste_ts + data
        i, n = 0, len(data)
        while i + 188 <= n:
            if data[i] != 0x47:
                j = data.find(b"\x47", i + 1)
                i = n if j == -1 else j
                continue
            self._paquet(data[i:i + 188])
            i += 188
        self._reste_ts = data[i:]
        return self._extraire_nal_completes()

    def _paquet(self, paquet: bytes) -> None:
        pusi = bool(paquet[1] & 0x40)
        pid = ((paquet[1] & 0x1F) << 8) | paquet[2]
        adaptation = (paquet[3] >> 4) & 0x3
        if adaptation not in (0x1, 0x3):
            return
        offset = 4 if adaptation == 0x1 else 5 + paquet[4]
        if offset >= len(paquet):
            return
        payload = paquet[offset:]

        if pid == 0x0000:
            self._pat(payload, pusi)
        elif self.pmt_pid is not None and pid == self.pmt_pid:
            self._pmt(payload, pusi)
        elif self.video_pid is not None and pid == self.video_pid:
            if pusi:
                # New PES packet: its first bytes are its header
                # (00 00 01 + stream_id + length + flags + optional data
                # length), to be skipped before reaching the
                # H.264 elementary data itself. The PTS, when
                # present, is there too.
                if len(payload) >= 9 and payload[0:3] == b"\x00\x00\x01":
                    pts = self._pts_depuis_entete_pes(payload)
                    if pts is not None:
                        self._marques_pts.append(
                            (len(self._flux_elementaire), pts)
                        )
                    self._flux_elementaire += payload[9 + payload[8]:]
            else:
                self._flux_elementaire += payload

    @staticmethod
    def _pts_depuis_entete_pes(payload: bytes) -> Optional[int]:
        """Raw PTS (33 bits, 90 kHz clock) from the optional PES header,
        if the PTS_DTS_flags flag announces it present (ITU-T H.222.0,
        2.4.3.7, table 2-21)."""
        if len(payload) < 14:
            return None
        pts_dts_flags = (payload[7] >> 6) & 0x3
        if pts_dts_flags == 0:
            return None
        b = payload[9:14]
        return (
            ((b[0] >> 1) & 0x07) << 30
            | b[1] << 22
            | (b[2] >> 1) << 15
            | b[3] << 7
            | (b[4] >> 1)
        )

    @staticmethod
    def _section(payload: bytes, pusi: bool):
        if not pusi or not payload:
            return None
        pointeur = payload[0]
        section = payload[1 + pointeur:]
        if len(section) < 3:
            return None
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        return section, section_length

    def _pat(self, payload: bytes, pusi: bool) -> None:
        trouve = self._section(payload, pusi)
        if trouve is None:
            return
        section, section_length = trouve
        i, fin = 8, 3 + section_length - 4  # -4: CRC32 at end of section
        while i + 4 <= fin and i + 4 <= len(section):
            program_number = (section[i] << 8) | section[i + 1]
            pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
            if program_number != 0 and self.pmt_pid is None:
                self.pmt_pid = pid
            i += 4

    def _pmt(self, payload: bytes, pusi: bool) -> None:
        if self.video_pid is not None:
            return
        trouve = self._section(payload, pusi)
        if trouve is None:
            return
        section, section_length = trouve
        # A PMT includes at least the fixed header and its CRC. Ignore
        # an incomplete table before any access or video PID choice.
        if section[0] != 0x02 or section_length < 13 or len(section) < 3 + section_length:
            return
        program_info_length = ((section[10] & 0x0F) << 8) | section[11]
        i, fin = 12 + program_info_length, 3 + section_length - 4
        video_pid = None
        while i + 5 <= fin and i + 5 <= len(section):
            stream_type = section[i]
            elementary_pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
            es_info_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
            if i + 5 + es_info_length > fin:
                return
            if stream_type == 0x1B and video_pid is None:
                video_pid = elementary_pid
            i += 5 + es_info_length
        if i == fin:
            self.video_pid = video_pid

    def _pts_a(self, position: int) -> Optional[int]:
        """Last PTS marked at a position <= `position` (without
        consuming it: several NAL units of the same frame share it)."""
        pts = None
        for p, v in self._marques_pts:
            if p <= position:
                pts = v
            else:
                break
        return pts

    def _extraire_nal_completes(self) -> list:
        """Emits every NAL unit whose end is known (a next start
        code has been seen) with its PTS, keeps the last - potentially
        still incomplete - in the buffer for the next call."""
        buf = self._flux_elementaire
        premier = buf.find(b"\x00\x00\x01")
        if premier == -1:
            return []
        resultats = []
        debut_nal = premier + 3
        suivant = buf.find(b"\x00\x00\x01", debut_nal)
        while suivant != -1:
            fin = suivant
            # 4-byte start code: the 0x00 just before belongs to the
            # separator, not to this NAL unit.
            if fin > debut_nal and buf[fin - 1] == 0:
                fin -= 1
            if fin > debut_nal:
                pts = self._pts_a(debut_nal - 3)
                resultats.append(
                    (pts, bytes(b"\x00\x00\x00\x01" + buf[debut_nal:fin]))
                )
            debut_nal = suivant + 3
            suivant = buf.find(b"\x00\x00\x01", debut_nal)
        limite = debut_nal - 3
        del buf[:limite]
        # Also keep the last marker before `limite`, repositioned at 0: it
        # remains the current PTS for the retained NAL (potentially
        # incomplete, at the head of the buffer after the cut). Without it, a NAL
        # too large to complete in a single alimenter() (a key frame
        # far exceeds 65536 bytes) lost its PTS as soon as this
        # marker was wrongly treated as stale (observed in the wild: only
        # the largest frames, never the small ones that complete in one
        # go - jerky playback, 2026-09-03).
        report = None
        gardees = []
        for p, v in self._marques_pts:
            if p < limite:
                report = v
            else:
                gardees.append((p - limite, v))
        if report is not None:
            gardees.insert(0, (0, report))
        self._marques_pts = gardees
        return resultats

    def finaliser(self) -> list:
        """Forces extraction of the very last NAL unit still in
        the buffer, never emitted by _extraire_nal_completes() for lack of a
        next start code to confirm its end - which will never come
        since the TS stream stops there. Call once only, at
        the normal end of the stream (otherwise the last received frame stays
        lost in this buffer, never passed to display or
        recording)."""
        buf = self._flux_elementaire
        premier = buf.find(b"\x00\x00\x01")
        debut_nal = premier + 3
        if premier == -1 or debut_nal >= len(buf):
            return []
        pts = self._pts_a(debut_nal - 3)
        resultat = [(pts, bytes(b"\x00\x00\x00\x01" + buf[debut_nal:]))]
        self._flux_elementaire = bytearray()
        self._marques_pts = []
        return resultat


def type_nal(nal_avec_start_code: bytes) -> int:
    """nal_unit_type (low 5 bits) of the first byte after the start code."""
    i = 3 if nal_avec_start_code[2] == 1 else 4
    return nal_avec_start_code[i] & 0x1F


def debut_unite_acces(nal_avec_start_code: bytes) -> bool:
    """True if this NAL necessarily opens a new access unit when
    the current unit already contains a slice (ITU-T H.264 §7.4.1.2.3).

    SEI, SPS, PPS, AUD and types 14 to 18 always precede the first
    slice of their frame. A slice (types 1 and 5) opens a new
    frame if first_mb_in_slice is 0, coded ue(v) by the single « 1 » bit:
    first bit after the NAL header. Later slices of the same
    frame (first_mb_in_slice > 0) start with a 0 bit."""
    i = 3 if nal_avec_start_code[2] == 1 else 4
    type_ = nal_avec_start_code[i] & 0x1F
    if type_ in (6, 7, 8, 9) or 14 <= type_ <= 18:
        return True
    if type_ in (1, 5):
        return len(nal_avec_start_code) > i + 1 and bool(nal_avec_start_code[i + 1] & 0x80)
    return False
