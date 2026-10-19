"""WebRTC live view: passthrough bridge between the Blink stream (immis,
via blinkpy) and a browser, as an alternative to /live-mse (MSE + ffmpeg).

Prototype measurement on 2026-09-03 (see BACKLOG.md): first frame 2.3 to
2.9x faster than MSE, without the CPU cost of re-encoding - MSE must
wait for the SPS to write an MP4 header (empty_moov) before emitting anything,
WebRTC negotiates codec parameters separately (SDP), so it can
start from the first key frame.

Optional by design: DISPONIBLE stays False if aiortc is not
installed (pip install -r requirements.txt not done yet, or platform where
the binary wheel does not exist). serve.py must fall back to MSE in that case,
never fail its own import because of this module.

Real pitfalls encountered while building this, all handled here:
- PyAV (av.open()) extracts SPS/PPS into codec_context.extradata instead of
  leaving them in the demuxed packet stream, and its probe (analyzeduration/
  probesize) must stay generous enough to capture them reliably -
  lowering that threshold blindly failed in the wild on jardin (2026-09-03):
  SDP negotiation succeeded, but incomplete extradata, no frame
  ever decoded. Hence blink_ts_demux.py: a minimal but correct MPEG-TS/PES/NAL
  demultiplexer (PAT -> PMT -> video PID -> elementary stream), which
  extracts SPS/PPS from the very first bytes, without the safety margin
  that had to be kept for PyAV's container probe.
- aiortc only announces H.264 Baseline in SDP (42001f/42e01f, hard-coded
  in aiortc/codecs/__init__.py) whereas Blink cameras film in
  High (often 640028): the browser negotiates Baseline, receives High,
  silently ignores everything. No official aiortc-side solution (issue
  aiortc/aiortc#944, closed without follow-up; the maintainer's reservation is about
  encoding, not this passthrough case). Hence _enregistrer_profil_h264,
  which adds the real profile (read from the SPS) to aiortc's capability
  table before answering - an undocumented implementation detail,
  liable to break on a future aiortc update without warning.
- RTCPeerConnection() without configuration falls back on a public STUN server
  by default (stun.l.google.com): setLocalDescription() only returns
  after candidate gathering; a network/firewall block on it
  left MODULE_SLOT/the disk lock held indefinitely in the wild
  (2026-09-03). Hence iceServers=[] (browser and server on the same
  local network, STUN has nothing to add anyway) and the two
  hard ceilings (NEGOCIATION_MAX_SECONDS, SESSION_MAX_SECONDS) below,
  same principles as LIVE_FIRST_FRAME_SECONDS/LIVE_MAX_SECONDS in MSE."""

import asyncio
from contextlib import suppress
import time
from fractions import Fraction
from pathlib import Path
from typing import Awaitable, Callable, Optional

import runtime

try:
    import av
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.codecs import CODECS
    from aiortc import rtp as _rtp
    from aiortc.mediastreams import MediaStreamError
    from aiortc.sdp import H264Profile, SessionDescription, parse_h264_profile_level_id
    from aiortc.rtcrtpparameters import (
        RTCRtcpFeedback,
        RTCRtpCodecCapability,
        RTCRtpCodecParameters,
    )
    import blink_ts_demux
    DISPONIBLE = True
except ImportError:
    DISPONIBLE = False

# Same hard ceilings as MSE (LIVE_FIRST_FRAME_SECONDS/LIVE_MAX_SECONDS,
# serve.py), for the same reason: without a bound, a network stall or a
# session never closed cleanly holds MODULE_SLOT/the disk lock
# indefinitely. NEGOCIATION_MAX_SECONDS stays generous: SDP offer/answer and
# DTLS handshake normally have nothing long to wait for.
NEGOCIATION_MAX_SECONDS = 15
# Distinct from NEGOCIATION_MAX_SECONDS since 2026-09-03 (BACKLOG.md):
# the two previously shared the same 15s ceiling for waiting on
# SPS/PPS (sps_pps_pret), conflating two unrelated things—the speed
# of a purely local SDP/DTLS negotiation, and the time a
# battery-powered camera takes to wake. 15s is enough for the first, not
# always for the second (timeout observed in the wild on jardin): same
# rationale and value as LIVE_FIRST_FRAME_SECONDS on the MSE side (serve.py), which
# already allows that patience for the same reason.
PREMIERE_IMAGE_MAX_SECONDS = 40
# Silence during an active session (after the first SPS/PPS, so once
# connectionState has already moved to "connected"): distinct from SESSION_MAX_SECONDS
# (420s), which bounds total duration of a healthy live stream, not how long we
# tolerate without a single byte. Without this ceiling, _lire() (below)
# stays blocked on its `await reader.read()` as long as the Blink relay does not
# cleanly close the TCP connection—never guaranteed (WinError 10054
# "connection closed by remote host" seen in asyncio logs AFTER
# the fact, not at the moment of the block). With no explicit signal, aiortc
# notices nothing on its side (no "disconnected" state, cf. above):
# connectionState stayed at "connected" until the only safety net left,
# SESSION_MAX_SECONDS—5 minutes stuck on "Reconnecting..." for
# the user (observed in the wild, Salon, 2026-09-04). Same order of
# magnitude as ATTENTE_HUB_MAX_SECONDS/the _stop_stream timeout (serve.py,
# 20s each) rather than an invented value here.
SILENCE_FLUX_MAX_SECONDS = 20
# Same value and same rationale as LIVE_MAX_SECONDS (serve.py): Blink itself
# closes a session after about six minutes of video (measured
# 2026-09-28), seven minutes let it run to the end.
SESSION_MAX_SECONDS = 420
FERMETURE_MAX_SECONDS = 10

# Read buffer (jitter buffer) before emitting the very first frame,
# then each following frame paced on its PTS from that same
# anchor. Measured in the wild on jardin (battery camera, 2026-09-03, cf.
# BACKLOG.md): a regular network gap of about 1.0s (0.62 to 0.86s measured
# over 6 cycles), very likely the camera Wi-Fi sleeping in
# intervals to save battery—the camera PTS stays
# perfectly regular (30 fps with no jumps), so it is not an encoding
# problem. Without buffering, recv() returned a frame as soon as
# demux: the slightest gap showed up directly as playback
# stutter (reported by the user). Cost: some of the
# latency gained over MSE, which still stays well ahead (jardin
# ~5.6s + 1.2s vs 15.8s on MSE). Applied uniformly (not only
# to battery cameras): simpler, and Salon has plenty of headroom to
# absorb it without becoming slower than MSE.
TAMPON_LECTURE_SECONDS = 1.2
# Memory guard if the browser stops consuming frames. Do not
# arbitrarily drop H.264 frames: later ones may depend on them.
FILE_IMAGES_MAX = 900
FILE_IMAGES_MAX_OCTETS = 32 * 1024 * 1024
TAMPON_ENREGISTREMENT_MAX_OCTETS = 8 * 1024 * 1024


if DISPONIBLE:

    # Recent Chromium also uses 35..63 when 96..127 is full.
    # aiortc 1.9 (and some recent versions) do not treat them as
    # dynamic: its answer then keeps the local PT instead of the
    # offer's, and the browser drops received RTP (zero frames).
    # RFC 5761 §4 allows this range, but forbids 64..95 (RTCP).
    _rtp.DYNAMIC_PAYLOAD_TYPES = frozenset(_rtp.DYNAMIC_PAYLOAD_TYPES) | frozenset(range(35, 64))

    class _PisteH264(MediaStreamTrack):
        """Video track without decode or re-encode: reads the local TCP stream
        from blinkpy byte by byte, demuxes ourselves (no PyAV,
        blink_ts_demux.py), groups NAL units by access unit
        (AUD, type 9, when the camera emits them like original Minis;
        otherwise first slice of a new frame, cf. _traiter_nal),
        passes them through as av.Packet.

        recv() may return an av.Packet rather than an av.VideoFrame
        (documented in MediaStreamTrack.recv's own docstring):
        RTCRtpSender (_next_encoded_frame, rtcrtpsender.py) then detects
        that it is not a Frame and calls encoder.pack() instead of
        encoder.encode(), which only splits the already
        compressed bitstream into RTP packets. Zero encoder calls. WebRTC
        equivalent of the -c:v copy already used by /live-mse.

        Timestamps: the real PTS encoded by the camera (extracted from the PES
        header by blink_ts_demux, already at 90 kHz—the same clock as
        RTP video, no conversion needed), not arrival time. An
        improvised timestamp on receive was tried then removed
        (2026-09-03): the network delivers in bursts, not at a steady rate,
        and the receiver then loses the timing information it needs
        to smooth playback—stutter observed in the wild. Fall back
        to the local clock only if a PTS were ever missing
        (should not happen: the camera provides one per frame).

        recv() also paces its output on that PTS, held behind
        TAMPON_LECTURE_SECONDS (cf. above) rather than returning a
        frame as soon as it is demuxed: needed to absorb the
        periodic network gap measured on battery cameras."""

        kind = "video"

        def __init__(
            self, reader: asyncio.StreamReader, ffmpeg: Optional[str] = None,
            chemin_enregistrement: Optional[Callable[[], Path]] = None,
            enregistrement_actif: Optional[Callable[[], bool]] = None,
            journal: Optional[Callable[[str], None]] = None,
        ) -> None:
            super().__init__()
            self._reader = reader
            self._demux = blink_ts_demux.DemuxeurTSVideo()
            self._t0 = time.monotonic()
            self._t_ancrage: Optional[float] = None
            self._pts_ancrage: Optional[int] = None
            self._unite_courante: list = []
            self._unite_courante_octets = 0
            self._unite_a_vcl = False
            self._pts_courant = None
            self._file: asyncio.Queue = asyncio.Queue(maxsize=FILE_IMAGES_MAX)
            self._file_octets = 0
            self.sps_pps = b""
            self.sps_pps_pret = asyncio.Event()
            # Set by negocier() after SPS/PPS: any end of the stream must
            # also close the connection, because track.stop() is not enough to
            # transition connectionState.
            self.pc = None
            self._demander_fermeture = None
            # Optional disk recording, button-driven: see
            # _synchroniser_enregistrement. chemin_enregistrement is a
            # callable (not an already-resolved path): each start must
            # recompute a fresh timestamp, not reuse the one from session
            # start.
            self._ffmpeg = ffmpeg
            self._chemin_enregistrement = chemin_enregistrement
            self._enregistrement_actif = enregistrement_actif
            # No-op by default: negocier() without an identified camera (tests,
            # future callers) must never crash for lack of journal.
            self._journal = journal or (lambda _message: None)
            self._recorder_process = None
            self._recorder_stdin = None
            self._recorder_demande = False
            self._recorder_demarrage = None
            self._recorder_attente = []
            self._recorder_attente_octets = 0
            self._fermee = False
            self._tache = asyncio.ensure_future(self._lire())

        async def _lire(self) -> None:
            fin_normale = False
            try:
                while not self._fermee:
                    # None (no ceiling) while the initial SPS/PPS has
                    # not yet arrived: PREMIERE_IMAGE_MAX_SECONDS (40s, negocier())
                    # already allows that longer patience by design (battery
                    # camera wake)—a shorter ceiling here would cut off in
                    # silence before that more generous delay gets a chance.
                    delai = (
                        SILENCE_FLUX_MAX_SECONDS
                        if self.sps_pps_pret.is_set() else None
                    )
                    try:
                        morceau = await asyncio.wait_for(
                            self._reader.read(65536), timeout=delai
                        )
                    except asyncio.TimeoutError:
                        self._journal(
                            f"Blink stream silent for "
                            f"{SILENCE_FLUX_MAX_SECONDS}s, closing"
                        )
                        fin_normale = True
                        break
                    if not morceau:
                        fin_normale = True
                        break
                    for pts, nal in self._demux.alimenter(morceau):
                        self._traiter_nal(pts, nal)
            except Exception as error:
                self._journal(
                    f"stream read interrupted, "
                    f"{type(error).__name__}: {error}"
                )
            finally:
                # All stream ends (EOF, reset, silence, cancellation)
                # must also close WebRTC; stopping the track alone does not
                # change connectionState or release the Blink module.
                try:
                    if fin_normale:
                        # The demuxer always keeps its very last
                        # NAL buffered, lacking a next start code to
                        # confirm its end (which will never come); without this
                        # flush, it—and the in-progress access unit
                        # waiting for it, never closed by a new AUD—were
                        # lost (real bug: last frame of a live stream
                        # never sent, neither to screen nor recording).
                        for pts, nal in self._demux.finaliser():
                            self._traiter_nal(pts, nal)
                        self._flush_unite_courante()
                    self._arreter_enregistrement()
                finally:
                    # vider=False (normal end) lets recv() consume
                    # frames already queued before hitting the end
                    # sentinel—without that, end of stream wiped everything
                    # still pending, never sent (real bug: on a
                    # 60-frame source, only those already read by the
                    # browser at EOF survived). Forced stop
                    # (fermer(), below) keeps immediate drain:
                    # the caller will not read recv() anyway.
                    self._terminer_file(vider=not fin_normale)
                    # A normal end lets recv() send frames
                    # already received before closing the connection.
                    if not fin_normale and self._demander_fermeture is not None:
                        self._demander_fermeture()

        def _traiter_nal(self, pts, nal: bytes) -> None:
            # Start of access unit per ITU-T H.264 §7.4.1.2.3, not
            # only AUD: original Blink Minis emit one per
            # frame, Mini 2 / Mini 2K+ apparently do not (issue #18). Without
            # AUD, nothing was ever sent to the browser—MSE, which
            # delegates this splitting to ffmpeg, worked on those same cameras.
            type_ = blink_ts_demux.type_nal(nal)
            if type_ == 9 or (
                self._unite_a_vcl and blink_ts_demux.debut_unite_acces(nal)
            ):
                self._flush_unite_courante()
            if not self._unite_courante:
                self._pts_courant = pts
            if type_ in (1, 5):
                self._unite_a_vcl = True
            self._unite_courante_octets += len(nal)
            if self._unite_courante_octets > FILE_IMAGES_MAX_OCTETS:
                raise RuntimeError("H.264 access unit with no detectable end")
            self._unite_courante.append(nal)
            if type_ in (7, 8) and not self.sps_pps_pret.is_set():
                self.sps_pps += nal
                if type_ == 8:  # PPS always follows SPS: both
                    self.sps_pps_pret.set()  # are here

        def _flush_unite_courante(self) -> None:
            if not self._unite_courante:
                return
            unite = b"".join(self._unite_courante)
            self._mettre_unite(self._pts_courant, unite)
            self._synchroniser_enregistrement(unite)
            self._unite_courante = []
            self._unite_courante_octets = 0
            self._unite_a_vcl = False

        def _mettre_unite(self, pts, unite: bytes) -> None:
            if (self._file.full()
                    or self._file_octets + len(unite) > FILE_IMAGES_MAX_OCTETS):
                raise RuntimeError("The browser is no longer consuming the video stream")
            self._file.put_nowait((pts, unite))
            self._file_octets += len(unite)

        def _terminer_file(self, *, vider: bool = True) -> None:
            # Releases bytes and wakes recv(), even if reading was
            # cancelled before its very first loop iteration.
            if vider:
                while not self._file.empty():
                    self._file.get_nowait()
                self._file_octets = 0
            try:
                self._file.put_nowait(None)
            except asyncio.QueueFull:
                # Can only happen if FILE_IMAGES_MAX (900) frames
                # are already waiting with none ever consumed—
                # _mettre_unite() already refuses a 901st before
                # getting here. best-effort: recv() will eventually read an
                # empty queue without sentinel rather than crash
                # cleanup (pc close, Blink module release).
                pass

        async def recv(self):
            item = await self._file.get()
            if item is None:
                self.stop()
                if self._demander_fermeture is not None:
                    self._demander_fermeture()
                raise MediaStreamError
            pts, donnees = item
            self._file_octets -= len(donnees)
            if pts is None:
                pts = int((time.monotonic() - self._t0) * 90000)

            # Anchor set on the very first frame, then each frame
            # waits its turn by PTS delta from that anchor—not
            # by arrival order. A network catching up after a gap by
            # delivering several frames at once does not therefore
            # emit them all at once: they stay spaced as
            # at capture time.
            if self._t_ancrage is None:
                self._t_ancrage = time.monotonic()
                self._pts_ancrage = pts
            cible = (
                self._t_ancrage + TAMPON_LECTURE_SECONDS
                + (pts - self._pts_ancrage) / 90000
            )
            attente = cible - time.monotonic()
            if attente > 0:
                await asyncio.sleep(attente)

            paquet = av.Packet(donnees)
            paquet.pts = pts
            paquet.time_base = Fraction(1, 90000)
            return paquet

        def _synchroniser_enregistrement(self, unite: bytes) -> None:
            """Starts/stops the recording subprocess according to the state
            requested by the page button (enregistrement_actif), then sends it
            this access unit if recording is active.

            Polled on each complete access unit (several times per
            second): a button click therefore shows up here within at most a fraction
            of a second, with no queue or dedicated
            event—same mechanism as the ENREGISTREMENT_DIRECT_
            ACTIF flag on the MSE side (serve.py), read on each block sent to the browser."""
            if self._enregistrement_actif is None or self._chemin_enregistrement is None:
                return
            desire = self._enregistrement_actif()
            if (desire and not self._recorder_demande
                    and self.sps_pps_pret.is_set()
                    and (self._recorder_demarrage is None
                         or self._recorder_demarrage.done())):
                # An MP4 started with a dependent frame is not
                # decodable. Wait for the next IDR, without re-encoding.
                depart = 0
                while True:
                    depart = unite.find(b"\x00\x00\x01", depart)
                    if depart < 0:
                        return
                    depart += 3
                    if depart < len(unite) and unite[depart] & 0x1F == 5:
                        break
                self._recorder_demande = True
                self._recorder_attente = [unite]
                self._recorder_attente_octets = len(unite)
                self._recorder_demarrage = asyncio.create_task(
                    self._demarrer_enregistrement()
                )
                return
            elif not desire and self._recorder_demande:
                self._arreter_enregistrement()
                return  # already stopped: nothing to send for this unit
            if self._recorder_demande and self._recorder_stdin is None:
                # create_subprocess_exec yields to the loop: all
                # units received in that interval must follow the IDR.
                if (self._recorder_attente_octets + len(unite)
                        > TAMPON_ENREGISTREMENT_MAX_OCTETS):
                    self._journal("recording stopped: startup too slow")
                    self._arreter_enregistrement()
                    return
                self._recorder_attente.append(unite)
                self._recorder_attente_octets += len(unite)
                return
            self._ecrire_enregistrement(unite)

        async def _demarrer_enregistrement(self) -> None:
            """Starts a dedicated ffmpeg that remuxes (without re-encoding) the
            Annex B stream already demuxed here into fragmented MP4—same
            format as /live-mse (frag_keyframe+empty_moov, serve.py), for
            the same reason: readable even if cut abruptly (tab close,
            crash), without relying on a clean exit to write a valid final moov.

            Subprocess separate from RTCPeerConnection on purpose: if
            recording slows (slow disk...), it must never
            slow _lire() and thus the live view in the browser.

            Units arriving during startup are kept in
            _recorder_attente, from an IDR until stdin is adopted.
            """
            try:
                chemin = self._chemin_enregistrement()
                chemin.parent.mkdir(parents=True, exist_ok=True)
                processus = await asyncio.create_subprocess_exec(
                    self._ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "h264", "-i", "pipe:0",
                    "-c", "copy", "-f", "mp4",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                    str(chemin),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=runtime.SANS_FENETRE,
                )
            except Exception as error:
                # Allows a retry on the next pass when "desire" is true:
                # without this, a single failure (ffmpeg missing...) would block
                # all recording for the rest of the session.
                self._journal(
                    f"failed to start disk recording, "
                    f"{type(error).__name__}: {error}"
                )
                self._recorder_demande = False
                self._recorder_attente.clear()
                self._recorder_attente_octets = 0
                return
            if not self._recorder_demande or self.readyState == "ended":
                # Stopped (button, or track closed) while this subprocess
                # was starting: nobody wants it anymore, do not
                # adopt it—just let it exit cleanly.
                try:
                    processus.stdin.close()
                except Exception:
                    pass
                asyncio.ensure_future(_attendre_fin_enregistrement(processus))
                return
            self._recorder_process = processus
            self._recorder_stdin = processus.stdin
            self._ecrire_enregistrement(self.sps_pps)
            attente = self._recorder_attente
            self._recorder_attente = []
            self._recorder_attente_octets = 0
            for unite in attente:
                self._ecrire_enregistrement(unite)

        def _ecrire_enregistrement(self, donnees: bytes) -> None:
            # write() does not block. Capping its buffer protects memory
            # if ffmpeg/disk stops consuming, without slowing the live view.
            stdin = self._recorder_stdin
            if stdin is None:
                return
            try:
                if (stdin.transport.get_write_buffer_size() + len(donnees)
                        > TAMPON_ENREGISTREMENT_MAX_OCTETS):
                    raise BufferError("disk buffer full")
                stdin.write(donnees)
            except Exception as error:
                # One line even if _lire() calls _ecrire_enregistrement
                # several times per second: stdin becomes None on the
                # first error, so this block does not run again.
                self._journal(
                    f"disk recording interrupted (write), "
                    f"{type(error).__name__}: {error}"
                )
                self._arreter_enregistrement()

        def _arreter_enregistrement(self) -> None:
            self._recorder_demande = False
            self._recorder_attente.clear()
            self._recorder_attente_octets = 0
            stdin = self._recorder_stdin
            self._recorder_stdin = None
            if stdin is not None:
                try:
                    stdin.close()
                except Exception:
                    pass
            processus = self._recorder_process
            self._recorder_process = None
            if processus is not None:
                asyncio.ensure_future(_attendre_fin_enregistrement(processus))

        def fermer(self) -> None:
            # Python 3.8: wait_for may swallow cancel() if read() just
            # finished. The flag then forbids a new read.
            self._fermee = True
            self.stop()
            self._tache.cancel()
            self._terminer_file()
            try:
                self._arreter_enregistrement()
            except Exception as error:
                # Called from cleanup (negocier(), below), before
                # `await on_close()`: an uncaught exception
                # here would interrupt the coroutine before on_close,
                # which releases MODULE_SLOT—direct.log showed a real
                # case (2026-09-04, negotiated log never followed by normal
                # session release, lock held indefinitely).
                # Under pythonw (autostart), with no console, such an
                # exception would never be seen anywhere
                # without this line: that is exactly what this
                # journal must now make visible.
                self._journal(
                    f"failed to stop recording during "
                    f"shutdown, {type(error).__name__}: {error}"
                )

    async def _attendre_fin_enregistrement(processus) -> None:
        """Lets ffmpeg drain its buffer and exit on its own once
        stdin is closed (fermer(), above); beyond that, same ceiling (5s) as
        ffmpeg cleanup in send_live_mse (serve.py)."""
        try:
            await asyncio.wait_for(processus.wait(), timeout=5)
        except Exception:
            try:
                processus.kill()
                await asyncio.wait_for(processus.wait(), timeout=5)
            except Exception:
                pass

    def _profile_level_id(sps_pps_annexb: bytes) -> Optional[str]:
        """profile_idc/level_idc (e.g. "640028") from an Annex B SPS.

        These 3 bytes always follow the NAL header at the start of the SPS (ITU-T
        H.264, 7.3.2.1.1)."""
        i = sps_pps_annexb.find(b"\x00\x00\x01")
        if i == -1:
            return None
        i += 3
        if len(sps_pps_annexb) < i + 4 or (sps_pps_annexb[i] & 0x1F) != 7:
            return None
        return sps_pps_annexb[i + 1 : i + 4].hex()

    _PROFILS_ENREGISTRES: set = set()

    def _profil_reception_h264(offer_sdp: str, profil_source: str) -> str:
        """Prefer the source profile; accept Chromium's software High 4:4:4
        decoder for a High stream, without converting frames.

        These are receiver capabilities, never announcing Baseline
        for a High source. SPS and video bytes stay unchanged.
        ITU-T H.264.1 §6.5.7 requires this decoding of the High subset.
        """
        source, _ = parse_h264_profile_level_id(profil_source)
        profils = set()
        for media in SessionDescription.parse(offer_sdp).media:
            if media.kind != "video" or media.port == 0 or media.direction in ("sendonly", "inactive"):
                continue
            for codec in media.rtp.codecs:
                if (codec.mimeType.lower() != "video/h264"
                        or str(codec.parameters.get("packetization-mode", "0")) != "1"):
                    continue
                try:
                    profil, _ = parse_h264_profile_level_id(
                        codec.parameters.get("profile-level-id", "42e01f")
                    )
                    profils.add(profil)
                except ValueError:
                    continue
        if source in profils:
            return profil_source
        if (source in (H264Profile.PROFILE_HIGH, H264Profile.PROFILE_CONSTRAINED_HIGH)
                and H264Profile.PROFILE_PREDICTIVE_HIGH_444 in profils):
            return "f400" + profil_source[4:]
        return profil_source  # aiortc will raise incompatible-codecs error.

    def _enregistrer_profil_h264(profil: str) -> None:
        """Adds this profile-level-id to aiortc.codecs.CODECS["video"].

        get_capabilities() (aiortc/codecs/__init__.py) reads this
        list directly, filled once by init_codecs() with Baseline
        only. setCodecPreferences() validates its argument against this
        same list ("Codec is not in capabilities" otherwise)."""
        if profil in _PROFILS_ENREGISTRES:
            return
        _PROFILS_ENREGISTRES.add(profil)
        pt = 110 + 2 * len(_PROFILS_ENREGISTRES)
        CODECS["video"] += [
            RTCRtpCodecParameters(
                mimeType="video/H264", clockRate=90000, payloadType=pt,
                rtcpFeedback=[
                    RTCRtcpFeedback(type="nack"),
                    RTCRtcpFeedback(type="nack", parameter="pli"),
                    RTCRtcpFeedback(type="goog-remb"),
                ],
                parameters={
                    "level-asymmetry-allowed": "1",
                    "packetization-mode": "1",
                    "profile-level-id": profil,
                },
            ),
            RTCRtpCodecParameters(
                mimeType="video/rtx", clockRate=90000, payloadType=pt + 1,
                parameters={"apt": pt},
            ),
        ]


async def fermer_connexion(pc) -> None:
    """Closes WebRTC and waits for the associated Blink stream to be released.

    aiortc emits its close events without awaiting their coroutines.
    pc.close() alone therefore does not guarantee on_close() has finished.
    """
    demander = getattr(pc, "_blink_demander_fermeture", None)
    if demander is None:
        await pc.close()
    else:
        await asyncio.shield(demander())


async def negocier(
    stream_url: str, offer_sdp: str, offer_type: str,
    on_close: Callable[[], Awaitable[None]],
    ffmpeg: Optional[str] = None,
    chemin_enregistrement: Optional[Callable[[], Path]] = None,
    enregistrement_actif: Optional[Callable[[], bool]] = None,
    journal: Optional[Callable[[str], None]] = None,
):
    """Opens the Blink stream (no decode), negotiates WebRTC, returns
    (pc, sdp_reponse, type_reponse).

    `on_close` is awaited when the connection ends (state
    "closed" or "failed"): this is the only end signal available here, there
    is no HTTP request to keep open as in MSE—closing
    the tab closes RTCPeerConnection on the browser side, which propagates
    here via connectionstatechange. Called at most once.

    `chemin_enregistrement`/`enregistrement_actif`, with `ffmpeg`: if
    provided, a copy of the live stream is written to that path while the latter
    returns true—driven by the page button (see
    _PisteH264._synchroniser_enregistrement)."""
    if not DISPONIBLE:
        raise RuntimeError(
            "aiortc n'est pas installe (pip install -r requirements.txt)"
        )
    journal = journal or (lambda _message: None)

    host, port_str = stream_url.replace("tcp://", "").split(":")
    writer = None
    track = None
    pc = None
    tache_plafond = None
    tache_fermeture = None

    async def _fermer_ressources() -> None:
        # This single task is shielded from negocier() cancellation and
        # HTTP request cancellation. It owns cleanup until it finishes.
        if tache_plafond is not None:
            tache_plafond.cancel()
        try:
            if track is not None:
                try:
                    track.fermer()
                except Exception as error:
                    journal(f"fermeture piste : {type(error).__name__}: {error}")
                with suppress(asyncio.CancelledError, Exception):
                    await track._tache
            if writer is not None:
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=5)
                except Exception as error:
                    journal(f"fermeture TCP : {type(error).__name__}: {error}")
            if pc is not None:
                try:
                    await asyncio.wait_for(pc.close(), timeout=FERMETURE_MAX_SECONDS)
                except Exception as error:
                    journal(f"fermeture WebRTC : {type(error).__name__}: {error}")
        finally:
            await on_close()

    def _demander_fermeture():
        nonlocal tache_fermeture
        if tache_fermeture is None:
            tache_fermeture = asyncio.create_task(_fermer_ressources())
            # Spontaneous ends (EOF/failed state) have no caller
            # awaiting this task: consume and log their error.
            def _fin(tache):
                if not tache.cancelled() and tache.exception() is not None:
                    error = tache.exception()
                    journal(f"nettoyage WebRTC : {type(error).__name__}: {error}")
            tache_fermeture.add_done_callback(_fin)
        return tache_fermeture

    try:
        reader, writer = await asyncio.open_connection(host, int(port_str))
        track = _PisteH264(
            reader, ffmpeg=ffmpeg, chemin_enregistrement=chemin_enregistrement,
            enregistrement_actif=enregistrement_actif, journal=journal,
        )
        # Time for the camera to wake and deliver its
        # SPS/PPS—PREMIERE_IMAGE_MAX_SECONDS (40s, cf. above), not
        # NEGOCIATION_MAX_SECONDS (15s, for what follows, purely local).
        try:
            attente_sps = asyncio.create_task(track.sps_pps_pret.wait())
            try:
                terminees, _ = await asyncio.wait(
                    (attente_sps, track._tache),
                    timeout=PREMIERE_IMAGE_MAX_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not terminees:
                    raise asyncio.TimeoutError
                if track._tache.done():
                    raise RuntimeError("The Blink stream stopped before the first frame.")
            finally:
                attente_sps.cancel()
                with suppress(asyncio.CancelledError):
                    await attente_sps
        except asyncio.TimeoutError:
            # Without this, the user only saw a bare "TimeoutError: "
            # (observed in the wild, 2026-09-03)—RuntimeError displays as-is
            # on the page (send_offer_webrtc, serve.py), same handling
            # as the already-clear MSE messages for this case.
            raise RuntimeError(
                "The camera sent no image. Out of range of the "
                "module, asleep, or already busy with another session."
            ) from None

        # iceServers=[]: without this, aiortc falls back to its default
        # (stun:stun.l.google.com:19302, RTCIceGatherer.getDefaultIceServers)
        # and setLocalDescription() only returns once candidate
        # gathering finishes—a network/firewall block there blocks everything
        # indefinitely (observed in the wild, cf. BACKLOG.md). Browser and
        # server on the same local network: no NAT to traverse, STUN
        # has nothing to add here anyway.
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        track.pc = pc
        track._demander_fermeture = _demander_fermeture
        pc._blink_demander_fermeture = _demander_fermeture

        @pc.on("connectionstatechange")
        def _sur_changement_etat() -> None:
            journal(f"connectionstatechange -> {pc.connectionState}")
            if pc.connectionState in ("closed", "failed"):
                # Do not await pc.close() from its own event:
                # aiortc may still be closing transports.
                _demander_fermeture()

        sender = pc.addTrack(track)
        profil = _profile_level_id(track.sps_pps)
        journal(f"H.264 source {profil or 'SPS unreadable'}")
        if profil:
            profil_reception = _profil_reception_h264(offer_sdp, profil)
            if profil_reception != profil:
                journal(f"H.264 source {profil}, negotiated decoder {profil_reception}, no re-encode")
            profil = profil_reception
            _enregistrer_profil_h264(profil)
            transceiver = next(
                t for t in pc.getTransceivers() if t.sender is sender
            )
            transceiver.setCodecPreferences([
                RTCRtpCodecCapability(
                    mimeType="video/H264", clockRate=90000,
                    parameters={
                        "level-asymmetry-allowed": "1",
                        "packetization-mode": "1",
                        "profile-level-id": profil,
                    },
                )
            ])

        # Safety net: even without STUN, nothing guarantees
        # negotiation always finishes quickly (flaky network, unidentified
        # aiortc/aioice bug...). Without a ceiling here, a stall
        # keeps MODULE_SLOT/the disk lock held indefinitely—already
        # observed in the wild (cf. BACKLOG.md): no connectionstatechange
        # ever arrives if negotiation itself never completes.
        async def _echanger_sdp():
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer_sdp, type=offer_type)
            )
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

        await asyncio.wait_for(
            _echanger_sdp(), timeout=NEGOCIATION_MAX_SECONDS
        )
        if tache_fermeture is not None:
            raise RuntimeError("The Blink stream stopped during negotiation.")
    except (Exception, asyncio.CancelledError):
        # CancelledError does not inherit Exception. Outer budget
        # expiry must also close TCP and release the module.
        with suppress(Exception):
            await asyncio.shield(_demander_fermeture())
        raise

    # Second safety net, once negotiation succeeds: a session that never
    # closes cleanly (tab killed rather than closed, network drop
    # without notification...) must not hold the resource
    # indefinitely either—same principle as LIVE_MAX_SECONDS on MSE (serve.py).
    async def _plafond_duree() -> None:
        await asyncio.sleep(SESSION_MAX_SECONDS)
        if pc.connectionState not in ("closed", "failed"):
            journal(
                f"filet de {SESSION_MAX_SECONDS}s declenche, connectionState "
                f"restait a {pc.connectionState!r}"
            )
            await fermer_connexion(pc)

    tache_plafond = asyncio.create_task(_plafond_duree())

    return pc, pc.localDescription.sdp, pc.localDescription.type
