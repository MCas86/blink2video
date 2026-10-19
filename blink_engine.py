"""Download engine: one USB then cloud pass, looped or once.

Extracted from blink2video.py at step 8 (AUDIT-2026-08-13.md, section 20, 8.4).
Composes models (blink_models) and registry (blink_registre); knows nothing
of the CLI or session, which are supplied ready-made."""

import asyncio
import copy
import datetime as dt
import functools
import os
import time
from pathlib import Path
from typing import NamedTuple

import runtime

from aiohttp import ClientError
from blinkpy.blinkpy import Blink
from blinkpy import livestream as _blinkpy_livestream

import merge_daily as md

import blink_models
import blink_registre


async def _recv_corrige(self):
    """Replaces BlinkLiveStream.recv (blinkpy/livestream.py): reads frames
    with `readexactly` rather than `read`.

    `StreamReader.read(n)` returns as soon as any data is available, not
    necessarily the requested `n` bytes. A video packet split across two TCP
    segments (common, especially on large packets) is then read in several
    chunks; upstream code treats the first incomplete chunk as a dead connection
    and closes a perfectly healthy session—hence live views stopping after a
    few seconds with no real network error. `readexactly` waits for the
    requested size and only raises on a true connection end.

    Upstream fix proposed but not yet on PyPI:
    https://github.com/fronzbot/blinkpy/pull/1232 (confirmed with a regression
    test that reproduces this symptom exactly). Remove once a blinkpy release
    includes it."""
    try:
        _blinkpy_livestream._LOGGER.debug("Starting copy from target to clients")
        premier_video = True
        while not self.target_reader.at_eof():
            try:
                data = await self.target_reader.readexactly(9)
            except asyncio.IncompleteReadError:
                _blinkpy_livestream._LOGGER.debug(
                    "Target closed before a full 9-byte header"
                )
                break

            msgtype = data[0]
            sequence = int.from_bytes(data[1:5], byteorder="big")
            payload_length = int.from_bytes(data[5:9], byteorder="big")
            _blinkpy_livestream._LOGGER.debug(
                "Received packet: msgtype=%d, sequence=%d, payload_length=%d",
                msgtype, sequence, payload_length,
            )

            if payload_length <= 0:
                _blinkpy_livestream._LOGGER.debug(
                    "Invalid payload length: %d", payload_length
                )
                continue

            try:
                data = await self.target_reader.readexactly(payload_length)
            except asyncio.IncompleteReadError:
                _blinkpy_livestream._LOGGER.debug(
                    "Target closed before a full payload"
                )
                break

            if msgtype != 0x00:
                _blinkpy_livestream._LOGGER.debug(
                    "Skipping unsupported msgtype %d", msgtype
                )
                continue

            if data[0] != 0x47:
                _blinkpy_livestream._LOGGER.debug(
                    "Skipping video payload missing 0x47 at start"
                )
                continue

            if premier_video:
                premier_video = False
                # Diagnostic only, never critical: a unit test of this recv()
                # (test_blink2video_audit.py) builds a bare BlinkLiveStream via
                # __new__, without `camera`—an exception here (AttributeError on
                # self.camera, or anything else) must not propagate to the outer
                # except, which would cut the whole video stream right after the
                # first frame (seen in production: this unprotected block made
                # client.write() fail silently).
                # Marks end of the portion outside blink2video's control (cloud
                # ticket, camera radio wake, relay handshake). Compare timestamps
                # to _journal_direct (serve.py, same "[direct]" prefix for the
                # only delay we own—local proxy + ffmpeg spawn/analyze/remux).
                # Also writes to direct.log (runtime.ajouter_ligne): under
                # pythonw, print() alone goes nowhere observable.
                try:
                    horodatage = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    ligne = msg("direct_premier_octet", horodatage=horodatage,
                               camera=self.camera.name)
                    print(ligne, flush=True)
                    runtime.ajouter_ligne("direct.log", ligne)
                except Exception:
                    pass

            _blinkpy_livestream._LOGGER.debug("Sending %d bytes to clients", len(data))
            for writer in self.clients:
                if not writer.is_closing():
                    # ONE client disconnecting during drain() must not propagate
                    # to recv()'s except/finally: that would close target_writer
                    # and kill the stream for all other clients still connected.
                    # join() (blinkpy/livestream.py) alone removes that writer
                    # from self.clients.
                    try:
                        writer.write(data)
                        await writer.drain()
                    except OSError:
                        _blinkpy_livestream._LOGGER.debug(
                            "Client disconnected during drain, skipping it"
                        )

            await asyncio.sleep(0)
    except _blinkpy_livestream.ssl.SSLError as e:
        if e.reason != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY":
            _blinkpy_livestream._LOGGER.exception("SSL error while receiving data")
    except Exception:
        _blinkpy_livestream._LOGGER.exception("Error while receiving data")
    finally:
        self.target_writer.close()
        _blinkpy_livestream._LOGGER.debug("Receiving was aborted, aborting sending")


_blinkpy_livestream.BlinkLiveStream.recv = _recv_corrige

# Do not replace BlinkLiveStream.auth: Blink's video relay presents a
# self-signed certificate, and blinkpy therefore applies a CERT_NONE TLS context
# limited to that connection. Classic CA validation was tried in v0.10.6; it
# broke all live views with CERTIFICATE_VERIFY_FAILED before the first byte.
# Ordinary Blink API calls remain strictly validated by blink_auth.contexte_tls().


LIBELLES = {
    "en": {
        "direct_premier_octet": "[direct] {horodatage} {camera}: first video byte from the relay",
        "mp4_tronque": "MP4 truncated or unreadable after download",
        "cloud_inventaire_titre": "\n--- Cloud subscription inventory ---",
        "cloud_inventaire_resume":
            "  {total} clip(s) in the cloud, {doublons} already acquired "
            "elsewhere, {inedits} to fetch.",
        "cloud_section_titre": "\n=== SUBSCRIPTION CLOUD ===",
        "cloud_supprime_auto": "    Deleted from the cloud (camera set to auto-delete).",
        "cloud_suppression_echec": "    ! Failed to delete from the cloud (clip kept there).",
        "echec_categorie": "    Failed [{categorie}]: {detail}.",
        "cloud_termine":
            "  Done: {downloaded} downloaded, {adopted} adopted, "
            "{skipped} skipped, {failed} failed.",
        "telechargement_deja_en_cours": "Download already in progress ({erreur}).",
        "tour_interrompu":
            "Download round interrupted by an error, retrying next round: {erreur}",
        "stockage_local_titre": "\n=== LOCAL STORAGE: {nom} ===",
        "module_occupe": "  Module busy (live view or refresh in progress): {erreur}.",
        "indisponible": "  Unavailable: {erreur}.",
        "stockage_local_inventaire_titre": "\n--- Local storage inventory: {nom} ---",
        "destination": "  Destination: {output}",
        "incremental": "  Incremental: {nouveaux} new, {deja} already acquired.",
        "cloud_indisponible": "  Cloud unavailable: {type}: {erreur}",
        "usb_echec": "    Acquisition failed (exception: {type}).",
        "usb_echec_etape": "    Acquisition failed: stage={etape}, HTTP status={statut}.",
        "usb_echec_raison": "    Why validation refused the file: {raison}.",
        "sync_suppression_impossible":
            "    ! Could not delete from the Sync Module ({type}); "
            "clip kept there.",
        "sync_supprime_auto": "    Deleted from the Sync Module (camera set to auto-delete).",
        "sync_suppression_echec":
            "    ! Failed to delete from the Sync Module (clip kept there).",
        "usb_echec_final": "    Download failed.",
        "usb_termine":
            "  Done: {downloaded} downloaded, {skipped} already present, "
            "{failed} failed.",
        "nouveaux_clips": "\nNew clips: {n}",
        "notif_corps_singulier": "{n} new clip downloaded. Click to open.",
        "notif_corps_pluriel": "{n} new clips downloaded. Click to open.",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])



def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


class CloudResult(NamedTuple):
    """Stable tally of one cloud pass, without conflating its four outcomes."""

    downloaded: int = 0
    adopted: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def had_error(self) -> bool:
        return self.failed > 0

    @property
    def new_downloads(self) -> int:
        return self.downloaded


def hub_lock(owner: str, stale_after: int = 600, attente: int = 0):
    """Reserve the Sync Module. Kept here for existing callers.

    `attente` (0 by default, as before): passed through to runtime.verrou();
    see its docstring for why this parameter exists."""
    return runtime.verrou("hub", owner, stale_after, attente)


BusyError = runtime.BusyError


class _HubCloud:
    """Stand-in module when only the cloud responds."""

    def __init__(self, network_id):
        self.sync_id = network_id or ""
        self.network_id = network_id or ""


class _ProgressionTelechargement:
    """Single counter for one pass, shared by all its modules and sources.

    One item represents one logical clip. When the same event exists on USB and
    in the cloud, the cloud download is its fallback and does not inflate the
    total. ``[n/total]`` lines feed the UI SSE stream; the work file covers
    automatic downloads.
    """

    def __init__(self, total: int):
        self.total = max(0, int(total))
        self.fait = 0
        self._publier()
        if self.total:
            # Deterministic state before the first transfer. INNER, in serve.py,
            # interprets this line as exactly 0/total.
            print(f"  [1/{self.total}] 0%", flush=True)

    def _publier(self) -> None:
        runtime.travail(
            "Downloading clips", self.fait, self.total,
            cle="phase.download_clips",
        )

    def commencer(self, nom: str) -> None:
        if self.total:
            position = min(self.fait + 1, self.total)
            print(f"  [{position}/{self.total}] {nom}", flush=True)
        self._publier()

    def terminer(self) -> None:
        if self.fait < self.total:
            self.fait += 1
        self._publier()
        if self.total:
            # Explicit publish after result: previously the last state stayed at
            # N-1/N, then the work file vanished immediately.
            print(f"  [{self.fait}/{self.total}] 100%", flush=True)

    def finir(self) -> None:
        """Ensure a final N/N state, even after an error branch."""
        self.fait = self.total
        self._publier()


class _PlanUSB:
    def __init__(self, nom, sync, clips, pending, adopted):
        self.nom = nom
        self.sync = sync
        self.clips = clips
        self.pending = pending
        self.adopted = adopted
        self.jobs = []


class _PlanCloud:
    def __init__(self, clips=None, pending=None, adopted=0, skipped=0,
                 consommees=None):
        self.clips = clips or []
        self.pending = pending or []
        self.adopted = adopted
        self.skipped = skipped
        # Registry keys already linked to a clip by inventory, or adopted:
        # revalidation does not reassign them to another clip.
        self.consommees = set(consommees or ())


class _DownloadJob:
    """One logical clip, with priority USB source and cloud fallback."""

    def __init__(self, usb=None, cloud=None):
        self.usb = usb
        self.cloud = cloud
        self.done = False


class _ResultatPassage(NamedTuple):
    code: int
    execute: bool


def _signaler_echec_acquisition(etape: str, erreur=None) -> None:
    try:
        statut = getattr(erreur, "status", None)
        if not isinstance(statut, int) or not 100 <= statut <= 599:
            statut = "unknown"
        print(msg("usb_echec_etape", etape=etape, statut=statut), flush=True)
    except Exception:
        pass


def _signaler_raison_validation(chemin: Path) -> None:
    """State WHY clip validation failed (issue #49).

    Without this, « stage=validation » does not distinguish a truncated file
    from a missing probe or a line ffprobe rejects. The reason is stripped of
    paths and clip names (see merge_daily.raison_refus_mp4): this log ends up
    pasted in public issues."""
    try:
        print(msg("usb_echec_raison", raison=md.raison_refus_mp4(chemin)), flush=True)
    except Exception:
        pass


async def download_clip(blink: Blink, clip, target: Path, overwrite: bool) -> str:
    """Prepare then download a clip, never deleting it from the hub.

    Optional deletion (GitHub issue #1) is decided by the caller afterward:
    see un_passage() and runtime.lire_suppression_auto()."""
    # An unregistered file at the expected path must pass the same deep
    # validation as a new transfer before adoption.
    etape = "local"
    try:
        if target.exists():
            etape = "validation"
            if md.valid_mp4_complet(target) and not overwrite:
                return "skipped"
        etape = "local"
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        partial.unlink(missing_ok=True)
    except Exception as error:
        _signaler_echec_acquisition(etape, error)
        raise

    etape = "preparation"
    try:
        prepared = await clip.prepare_download(blink)
        if not prepared:
            _signaler_echec_acquisition(etape)
            return "failed"
        etape = "transfer"
        if not await clip.download_video(blink, str(partial)):
            _signaler_echec_acquisition(etape)
            return "failed"
        etape = "validation"
        # The probe walks .part packets before rename: intact ftyp/moov is not
        # enough if mdat was truncated in transit.
        if not partial.exists() or not md.valid_mp4_complet(partial):
            _signaler_echec_acquisition(etape)
            if partial.exists():
                _signaler_raison_validation(partial)
            return "failed"
        etape = "local"
        partial.replace(target)
        return "downloaded"
    except Exception as error:
        _signaler_echec_acquisition(etape, error)
        raise
    finally:
        try:
            partial.unlink(missing_ok=True)
        except Exception as error:
            _signaler_echec_acquisition("local", error)
            raise


async def _inventorier_cloud(blink: Blink, args, output: Path,
                             state: dict, modules: list) -> _PlanCloud:
    """Build the cloud plan without starting any transfer."""
    clips = await blink_models.read_cloud_manifest(blink, args.since)
    if args.camera:
        clips = [c for c in clips if c.name.casefold() == args.camera.casefold()]
    if args.hub:
        # --hub already filters USB modules (blink_cli.py, select_sync_modules);
        # without this filter, another network's cloud stayed in the plan
        # (downloaded, then saved with the requested hub name in its "hub"
        # field although it did not come from that hub). modules is already
        # reduced to the selected hub at this stage: keeping only clips whose
        # network_id matches one of these modules is hub filtering without the
        # cloud clip knowing a hub name itself.
        reseaux_autorises = {
            str(getattr(sync, "network_id", "") or "") for _, sync in modules
        }
        clips = [c for c in clips
                if str(getattr(c, "network_id", "") or "") in reseaux_autorises]
    if not clips:
        return _PlanCloud()

    print(msg("cloud_inventaire_titre"))
    # Matching uses what is already in the registry, not the USB manifest: the
    # latter only shows what the key still holds, while the registry records
    # everything that was fetched.
    acquises = [
        (cle, entree) for cle, entree in state["clips"].items()
        if blink_registre._entree_acquise(output, entree)
    ]
    connus = [blink_registre._ClipConnu(entree) for _, entree in acquises]
    tombstones = [
        blink_registre._ClipConnu(entree)
        for entree in state["clips"].values()
        if isinstance(entree, dict) and entree.get("excluded")
    ]
    # Two different cloud IDs are never the same event, even one second apart
    # (audit 26/09/2026, B01): tolerance matches different sources, or old
    # entries without an ID.
    meme_evenement = blink_registre.meme_evenement_cloud
    sans_tombstone = (
        blink_models.rapprocher(tombstones, clips, compatibles=meme_evenement)[0]
        if tombstones else list(clips)
    )
    # `sans_tombstone` holds clips without exclusion; an explicit decision
    # therefore stays priority even on forced re-download.
    clips_autorises = sans_tombstone
    ignores_exclus = len(clips) - len(clips_autorises)
    if args.overwrite:
        inedits, doublons, consommees = clips_autorises, [], set()
    else:
        inedits, doublons, paires = blink_models.rapprocher_paires(
            connus, clips_autorises, compatibles=meme_evenement,
        )
        # Each entry linked by the plan is linked once and for all: revalidation
        # does not reassign it to a neighboring clip.
        consommees = {acquises[indice][0] for indice, _ in paires}
    print(msg("cloud_inventaire_resume", total=len(clips), doublons=len(doublons),
              inedits=len(inedits)))
    if args.command != "download" or not inedits:
        return _PlanCloud(
            clips=clips,
            skipped=len(doublons) + ignores_exclus,
        )

    pending = []
    adopted = 0
    for clip in sorted(inedits, key=blink_models.clip_datetime_utc):
        sync = _HubCloud(clip.network_id)
        target = blink_models.target_path(output, clip, sync=sync, source="cloud")
        _, entree_connue = blink_registre._trouver_entree(
            state, sync, clip, consumed=consommees, source="cloud",
        )
        if (
            entree_connue is None
            and target.exists()
            and target.is_file()
            and md.valid_mp4_complet(target)
            and not args.overwrite
        ):
            blink_registre.remember_download(state, sync, args.hub or "", clip, output,
                              target, source="cloud")
            blink_registre.save_download_state(output, state)
            consommees.add(blink_registre.state_key(sync, clip, source="cloud"))
            adopted += 1
            continue
        pending.append(clip)

    return _PlanCloud(
        clips=clips,
        pending=pending,
        adopted=adopted,
        skipped=len(doublons) + ignores_exclus,
        consommees=consommees,
    )


JOURNAL_TELECHARGEMENTS = "telechargements.log"

# Thumbnails to build when clips arrive (see _preparer_vignettes): at most this
# many per pass; the rest, e.g. a first download of hundreds of clips, is built
# on page demand as before, without delaying the notification.
MAX_VIGNETTES_PAR_PASSAGE = 40
_VIGNETTES_EN_ATTENTE: list = []


@functools.lru_cache(maxsize=1)
def _ffmpeg_pour_vignettes():
    try:
        return md.find_ffmpeg()
    except RuntimeError:
        return None


def _journaliser_telechargement(source: str, target: Path, output: Path) -> None:
    """One line per downloaded clip: enough to tell later what a « N new clips »
    notification really meant.

    Time, source (usb or cloud), path relative to the clips folder and size: no
    URL or account identifier. Never fatal (runtime.ajouter_ligne)."""
    try:
        nom = target.relative_to(output).as_posix()
    except ValueError:
        nom = target.name
    try:
        taille = target.stat().st_size
    except OSError:
        taille = 0
    moment = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    runtime.ajouter_ligne(JOURNAL_TELECHARGEMENTS, f"{moment}  {source}  {nom}  {taille} o")
    _VIGNETTES_EN_ATTENTE.append((target, output))


def _preparer_vignettes() -> None:
    """Build thumbnails for clips that just arrived, same place and name as
    serve.py: the page finds them ready instead of launching ffmpeg per clip
    when opening the gallery (Joël cold test, PR #59: 2,672 launches for 2,672
    clips).

    Never fatal: a missing thumbnail is built on demand, as before. The folder
    is serve.py's default (beside the clips folder); with a custom --thumbs
    option, the thumbnail built here is simply unused. A thumbnail older than
    the normalized clip version is rebuilt later by serve.py."""
    en_attente = _VIGNETTES_EN_ATTENTE[:MAX_VIGNETTES_PAR_PASSAGE]
    del _VIGNETTES_EN_ATTENTE[:]
    ffmpeg = _ffmpeg_pour_vignettes() if en_attente else None
    if not ffmpeg:
        return
    for target, output in en_attente:
        try:
            identite = md.clip_identity(output, target)
            vignette = (output.parent / ".blink_thumbs" / "clip" / identite).with_suffix(".jpg")
            vignette.parent.mkdir(parents=True, exist_ok=True)
            provisoire = vignette.with_name(f"{vignette.stem}.{os.getpid()}.tmp.jpg")
            try:
                if md.extraire_vignette(ffmpeg, target, provisoire, timeout=60):
                    provisoire.replace(vignette)
            finally:
                provisoire.unlink(missing_ok=True)
        except Exception:
            continue


def _suppression_auto_autorisee(sync, clip) -> bool:
    """Re-read the choice after local copy, just before the remote call.

    Do not freeze this choice at batch start: disabling during a transfer must
    protect this clip and the following ones. No settings lock is held during
    the API; a request already in flight may still succeed.
    """
    return (blink_registre.camera_setting_key(sync, clip)
            in runtime.lire_suppression_auto())


async def _telecharger_cloud(blink: Blink, args, output: Path, state: dict,
                             plan: _PlanCloud, jobs: list,
                             progression: _ProgressionTelechargement) -> CloudResult:
    """Run cloud-only jobs or fallbacks for failed USB jobs."""
    if not plan.clips:
        return CloudResult()

    downloaded = failed = adopted_execution = skipped_execution = 0
    # A job already finished succeeded via USB: its cloud equivalent is a
    # duplicate, never a second clip to advance in the bar.
    skipped_usb = sum(1 for job in jobs if job.cloud is not None and job.done)
    jobs_a_executer = [
        job for job in jobs if job.cloud is not None and not job.done
    ]
    # A phase title puts the SSE bar back in indeterminate mode. Emit it only if
    # a real cloud operation will produce the next ticks; when all its clips
    # already came from USB, the bar should stay at N/N instead of ending on a
    # spinner.
    if jobs_a_executer:
        print(msg("cloud_section_titre"))

    # Entries already linked to a clip (audit 26/09/2026, B01): inventory plan
    # ones, then those this pass links along the way. A job finished via USB
    # tied its new USB entry to its cloud clip: it cannot absorb a neighboring
    # cloud clip the plan kept separate.
    consommees = set(plan.consommees)
    for job in jobs:
        if job.cloud is not None and job.done and job.usb is not None:
            plan_usb, clip_usb = job.usb
            consommees.add(blink_registre.state_key(plan_usb.sync, clip_usb))

    for job in jobs_a_executer:
        clip = job.cloud
        sync = _HubCloud(clip.network_id)
        cle_cloud = blink_registre.state_key(sync, clip, source="cloud")
        target = blink_models.target_path(output, clip, sync=sync, source="cloud")
        progression.commencer(target.name)
        resultat = "failed"
        partiel = target.with_suffix(target.suffix + ".part")
        partiel.unlink(missing_ok=True)
        try:
            # Last-moment revalidation: another path in the same pass may have
            # acquired the media since the inventory phase.
            cle_connue, entree_connue = blink_registre._trouver_entree(
                state, sync, clip, consumed=consommees, source="cloud",
            )
            if (
                isinstance(entree_connue, dict)
                and blink_registre._entree_acquise(output, entree_connue)
                and not args.overwrite
            ):
                consommees.add(cle_connue)
                resultat = "skipped"
            elif (
                entree_connue is None
                and target.exists()
                and target.is_file()
                and md.valid_mp4_complet(target)
                and not args.overwrite
            ):
                blink_registre.remember_download(
                    state, sync, args.hub or "", clip, output, target,
                    source="cloud",
                )
                blink_registre.save_download_state(output, state)
                consommees.add(cle_cloud)
                adopted_execution += 1
                resultat = "adopted"
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                telecharge = await clip.download_to(blink, partiel)
                if telecharge and md.valid_mp4_complet(partiel):
                    partiel.replace(target)
                    blink_registre.remember_download(
                        state, sync, args.hub or "", clip, output, target,
                        source="cloud",
                    )
                    blink_registre.save_download_state(output, state)
                    consommees.add(cle_cloud)
                    downloaded += 1
                    resultat = "downloaded"
                    _journaliser_telechargement("cloud", target, output)
                    runtime.notifier_nouveau_media(clip.name, target, "clip")
                    if _suppression_auto_autorisee(sync, clip):
                        if await clip.delete_video(blink):
                            print(msg("cloud_supprime_auto"))
                            state["clips"][
                                blink_registre.state_key(sync, clip, source="cloud")
                            ]["source_deleted"] = True
                            blink_registre.save_download_state(output, state)
                        else:
                            print(msg("cloud_suppression_echec"))
                else:
                    if telecharge:
                        clip.download_issue = (
                            blink_models.msg("cat_donnees"), msg("mp4_tronque"),
                        )
                    categorie, detail = getattr(
                        clip, "download_issue", None
                    ) or (blink_models.msg("cat_media"), blink_models.msg("contenu_indisponible"))
                    print(msg("echec_categorie", categorie=categorie, detail=detail))
        except (ClientError, OSError, asyncio.TimeoutError) as erreur:
            print(msg("echec_categorie", categorie=blink_models.msg("cat_reseau"),
                      detail=type(erreur).__name__))
        except Exception as erreur:  # Isolate one invalid clip from the rest.
            print(msg("echec_categorie", categorie=blink_models.msg("cat_donnees"),
                      detail=type(erreur).__name__))
        finally:
            partiel.unlink(missing_ok=True)

        if resultat == "failed":
            failed += 1
        elif resultat == "skipped":
            skipped_execution += 1
        job.done = True
        progression.terminer()

    resultat = CloudResult(
        downloaded=downloaded,
        adopted=plan.adopted + adopted_execution,
        skipped=plan.skipped + skipped_usb + skipped_execution,
        failed=failed,
    )
    print(msg("cloud_termine", downloaded=resultat.downloaded, adopted=resultat.adopted,
              skipped=resultat.skipped, failed=resultat.failed))
    return resultat


async def traiter_cloud(blink: Blink, args, modules: list) -> CloudResult:
    """Inventory then download cloud with a full 0/N–N/N counter.

    ``un_passage`` uses the same building blocks with USB jobs for a global
    total. This wrapper remains for tests and historical cloud-only callers.
    """
    output = args.output.resolve()
    state = blink_registre.load_download_state(output)
    if args.command == "download":
        runtime.travail(
            "Finding clips to download", 0, 0, cle="phase.inventory_clips",
        )
    plan = await _inventorier_cloud(blink, args, output, state, modules)
    if args.command != "download":
        return CloudResult(skipped=plan.skipped)
    jobs = [_DownloadJob(cloud=clip) for clip in plan.pending]
    progression = _ProgressionTelechargement(len(jobs))
    resultat = await _telecharger_cloud(
        blink, args, output, state, plan, jobs, progression,
    )
    progression.finir()
    return resultat


async def _faire_passage(blink: Blink, args, modules: list,
                         repetition: bool) -> _ResultatPassage:
    """Run one pass under its lock and always clean up its publication."""
    execute = False
    try:
        # One fetch at a time, regardless of source: two processes taking the
        # same clip would write the same partial file, and the first rename
        # would leave the other empty-handed.
        with runtime.verrou("download", "download", attente=30):
            execute = True
            try:
                code = await un_passage(blink, args, modules)
            finally:
                # The last N/N stays briefly visible to the web probe without
                # counting as still-active work.
                runtime.fin_travail(conserver=10)
            return _ResultatPassage(code, True)
    except runtime.BusyError as erreur:
        print(msg("telechargement_deja_en_cours", erreur=erreur))
        # In a loop, the other worker is already doing the job and the next
        # round will retry. For a manual click, returning success would start
        # merge and show « done » although no download from that click ran.
        return _ResultatPassage(0 if repetition else 1, False)
    except Exception as erreur:
        print(msg("tour_interrompu", erreur=erreur))
        return _ResultatPassage(1, execute)


async def _attendre_echeance(echeance: float) -> bool:
    """Wait until a deadline while checking for « stop » every second."""
    while not runtime.arret_demande():
        reste = echeance - time.monotonic()
        if reste <= 0:
            return True
        await asyncio.sleep(min(1.0, reste))
    return False


async def _boucler_sources(blink: Blink, args, modules: list,
                           usb_minutes: int, cloud_minutes: int) -> int:
    """Schedule USB and cloud together, with two independent cadences.

    The first round is always ``all``: after a storage change, inventory therefore
    knows all clips before the first byte transfers and publishes one denominator.
    Later rounds wake each source only on its own cadence; if both deadlines
    coincide, a new global pass keeps the same property.
    """
    periode_usb = usb_minutes * 60
    periode_cloud = cloud_minutes * 60
    # Until the lock is actually acquired, global bootstrap is not done. A
    # manual refresh present at startup must not turn the next real round into
    # cloud-only.
    while not runtime.arret_demande():
        debut = time.monotonic()
        premier = copy.copy(args)
        premier.source = "all"
        resultat = await _faire_passage(blink, premier, modules, repetition=True)
        if resultat.execute:
            break
        if not await _attendre_echeance(time.monotonic() + 1):
            return 0
    else:
        return 0
    echeance_usb = debut + periode_usb
    echeance_cloud = debut + periode_cloud
    if not modules:
        echeance_usb = float("inf")

    while not runtime.arret_demande():
        prochaine = min(echeance_usb, echeance_cloud)
        if not await _attendre_echeance(prochaine):
            break

        maintenant = time.monotonic()
        usb_du = maintenant >= echeance_usb
        cloud_du = maintenant >= echeance_cloud
        tour = copy.copy(args)
        tour.source = "all" if usb_du and cloud_du else ("usb" if usb_du else "cloud")
        await _faire_passage(blink, tour, modules, repetition=True)

        # Deadlines anchored at start, never at pass end: a long round skips
        # missed slots without permanently shifting the following ones.
        maintenant = time.monotonic()
        if usb_du:
            while echeance_usb <= maintenant:
                echeance_usb += periode_usb
        if cloud_du:
            while echeance_cloud <= maintenant:
                echeance_cloud += periode_cloud
    return 0


async def boucler(blink: Blink, args, modules: list) -> int:
    """Repeat requested passes while keeping the Blink session open.

    The session is opened once: at a fast cadence, reconnecting each round would
    cost more than the work itself and multiply authentications for no reason.

    Only BusyError was caught before: an HTTP outage, auth error, or unexpected
    Blink schema then escaped the loop and killed this background worker for good
    (I-17). We therefore broaden capture to any pass error, logging it, so a
    transient incident waits for the next pass instead of requiring a manual
    restart. Ctrl+C (KeyboardInterrupt) and task cancellation stay outside
    `Exception`: they still stop the loop."""
    usb_minutes = getattr(args, "usb_loop", None)
    cloud_minutes = getattr(args, "cloud_loop", None)
    if usb_minutes is not None and cloud_minutes is not None:
        return await _boucler_sources(
            blink, args, modules, usb_minutes, cloud_minutes,
        )

    while not runtime.arret_demande():
        echeance = time.monotonic() + (args.loop or 0) * 60
        resultat = await _faire_passage(
            blink, args, modules, repetition=bool(args.loop),
        )
        code = resultat.code
        if not args.loop:
            return code
        # Deadline from round start, not end: a round slower than the cadence
        # must not shift all following ones by that much (O-05). A too-slow
        # round simply skips its rest.
        if not await _attendre_echeance(echeance):
            break
    return 0


async def un_passage(blink: Blink, args, modules: list) -> int:
    """One round: inventory everything, then run one global deduplicated plan."""
    had_error = False
    neufs_total = 0

    # ``list`` has nothing to transfer: keep its light path and per-source
    # output without creating fake work in the UI.
    if args.command != "download":
        for name, sync in ([] if args.source == "cloud" else modules):
            print(msg("stockage_local_titre", nom=name))
            try:
                with hub_lock(name):
                    clips = await blink_models.read_local_manifest(sync)
            except runtime.BusyError as error:
                print(msg("module_occupe", erreur=error))
                had_error = True
                continue
            except RuntimeError as error:
                print(msg("indisponible", erreur=error))
                had_error = True
                continue
            clips = blink_models.filter_clips(clips, args.camera, args.since)
            blink_models.print_clip_summary(clips)
        if args.source != "usb":
            resultat_cloud = await traiter_cloud(blink, args, modules)
            had_error = resultat_cloud.had_error or had_error
        return 1 if had_error else 0

    output = args.output.resolve()
    state = blink_registre.load_download_state(output)
    runtime.travail("Finding clips to download", 0, 0,
                    cle="phase.inventory_clips")

    # Phase 1: all manifests and filters before the first byte transfers. Only
    # way to know a true global total.
    plans_usb = []
    for name, sync in ([] if args.source == "cloud" else modules):
        print(msg("stockage_local_inventaire_titre", nom=name))
        runtime.travail("Finding clips to download", 0, 0,
                        cle="phase.inventory_clips")
        try:
            with hub_lock(name):
                clips = await blink_models.read_local_manifest(sync)
        except runtime.BusyError as error:
            print(msg("module_occupe", erreur=error))
            had_error = True
            continue
        except RuntimeError as error:
            print(msg("indisponible", erreur=error))
            had_error = True
            continue

        clips = blink_models.filter_clips(clips, args.camera, args.since)
        blink_models.print_clip_summary(clips)
        print(msg("destination", output=output))
        pending = []
        adopted = 0
        index_registre = blink_registre._index_registre(state)
        correspondances = blink_registre._apparier_registre(
            state, sync, clips, index_registre,
        )
        for indice_clip, clip in enumerate(clips):
            target = blink_models.target_path(
                output, clip, sync=sync, source="usb",
            )
            _, entree_connue = correspondances.get(indice_clip, (None, None))
            if isinstance(entree_connue, dict) and entree_connue.get("excluded"):
                continue
            if (
                isinstance(entree_connue, dict)
                and blink_registre._entree_acquise(output, entree_connue)
                and not args.overwrite
            ):
                continue
            if (
                entree_connue is None
                and target.exists()
                and target.is_file()
                and md.valid_mp4_complet(target)
                and not args.overwrite
            ):
                blink_registre.remember_download(
                    state, sync, name, clip, output, target,
                )
                adopted += 1
                continue
            pending.append(clip)

        blink_registre.save_download_state(output, state)
        deja = len(clips) - len(pending)
        print(msg("incremental", nouveaux=len(pending), deja=deja))
        plans_usb.append(_PlanUSB(name, sync, clips, pending, adopted))

    plan_cloud = _PlanCloud()
    if args.source != "usb":
        runtime.travail("Finding clips to download", 0, 0,
                        cle="phase.inventory_clips")
        try:
            plan_cloud = await _inventorier_cloud(blink, args, output, state, modules)
        except Exception as error:
            # Momentarily unavailable cloud must not discard the already
            # inventoried USB plan: it will retry on the next pass.
            print(msg("cloud_indisponible", type=type(error).__name__, erreur=error))
            had_error = True

    # One job per USB clip; a correlated cloud clip becomes its fallback. Cloud
    # clips without a USB counterpart get their own jobs. ``--overwrite`` still
    # intentionally keeps both downloads, as before.
    jobs = []
    clips_usb = []
    sync_par_clip = {}
    for plan in plans_usb:
        for clip in plan.pending:
            job = _DownloadJob(usb=(plan, clip))
            plan.jobs.append(job)
            jobs.append(job)
            clips_usb.append(clip)
            sync_par_clip[id(clip)] = plan.sync

    cloud_apparies = set()
    if clips_usb and plan_cloud.pending and not args.overwrite:
        paires = blink_models._apparier_evenements(
            clips_usb,
            plan_cloud.pending,
            compatibles=lambda local, distant: blink_models._meme_camera(
                local, distant, sync_gauche=sync_par_clip.get(id(local)),
            ),
        )
        for indice_usb, indice_cloud in paires:
            jobs[indice_usb].cloud = plan_cloud.pending[indice_cloud]
            cloud_apparies.add(indice_cloud)
    for indice, clip in enumerate(plan_cloud.pending):
        if indice not in cloud_apparies:
            jobs.append(_DownloadJob(cloud=clip))

    progression = _ProgressionTelechargement(len(jobs))

    # Phase 2a: USB, grouped by module to use one source at a time. A failure
    # with a cloud equivalent keeps the same job unfinished: the fallback will
    # finish it without changing the denominator.
    for plan in plans_usb:
        if not plan.jobs:
            continue
        print(msg("stockage_local_titre", nom=plan.nom))
        downloaded = skipped = failed = echecs_sans_repli = 0
        try:
            with hub_lock(plan.nom):
                for job in plan.jobs:
                    clip = job.usb[1]
                    target = blink_models.target_path(
                        output, clip, sync=plan.sync, source="usb",
                    )
                    progression.commencer(target.name)
                    try:
                        resultat = await download_clip(
                            blink, clip, target, args.overwrite,
                        )
                    except Exception as error:
                        # Class name only: that is code, not data. Exception text
                        # may contain a private URL (account and network IDs),
                        # never printed (PR #53).
                        print(msg("usb_echec", type=type(error).__name__))
                        resultat = "failed"

                    if resultat == "downloaded":
                        downloaded += 1
                        blink_registre.remember_download(
                            state, plan.sync, plan.nom, clip, output, target,
                        )
                        blink_registre.save_download_state(output, state)
                        _journaliser_telechargement("usb", target, output)
                        runtime.notifier_nouveau_media(clip.name, target, "clip")
                        if _suppression_auto_autorisee(plan.sync, clip):
                            # Local copy is already valid and registered. A delete
                            # API failure must not undo this success, interrupt
                            # all following clips, or leave the bar before N/N.
                            try:
                                supprime = await clip.delete_video(blink)
                            except Exception as error:
                                print(msg("sync_suppression_impossible",
                                          type=type(error).__name__))
                            else:
                                if supprime:
                                    print(msg("sync_supprime_auto"))
                                    state["clips"][
                                        blink_registre.state_key(plan.sync, clip)
                                    ]["source_deleted"] = True
                                    blink_registre.save_download_state(output, state)
                                else:
                                    print(msg("sync_suppression_echec"))
                    elif resultat == "skipped":
                        skipped += 1
                        if target.exists() and md.valid_mp4(target):
                            blink_registre.remember_download(
                                state, plan.sync, plan.nom, clip, output, target,
                            )
                            blink_registre.save_download_state(output, state)
                    else:
                        failed += 1
                        if job.cloud is None:
                            echecs_sans_repli += 1
                        print(msg("usb_echec_final"))

                    if resultat != "failed" or job.cloud is None:
                        job.done = True
                        progression.terminer()
        except runtime.BusyError as error:
            print(msg("module_occupe", erreur=error))
            failed = len(plan.jobs)
            for job in plan.jobs:
                if job.done:
                    continue
                clip = job.usb[1]
                target = blink_models.target_path(
                    output, clip, sync=plan.sync, source="usb",
                )
                progression.commencer(target.name)
                if job.cloud is None:
                    echecs_sans_repli += 1
                    job.done = True
                    progression.terminer()

        print(msg("usb_termine", downloaded=downloaded, skipped=skipped, failed=failed))
        # A USB failure correlated to cloud is not final yet: the same job will
        # be tried next by the second source. Only fallback failure (counted by
        # resultat_cloud) should make the pass non-zero.
        had_error = had_error or echecs_sans_repli > 0
        neufs_total += downloaded

    # Phase 2b: cloud only or fallback for failed USB.
    if args.source != "usb":
        resultat_cloud = await _telecharger_cloud(
            blink, args, output, state, plan_cloud, jobs, progression,
        )
        had_error = resultat_cloud.had_error or had_error
        neufs_total += resultat_cloud.new_downloads

    progression.finir()

    if args.command == "download":
        # Thumbnails before notification: clicking the toast opens the page,
        # which must find them ready. Off the event loop (ffmpeg blocks);
        # run_in_executor rather than asyncio.to_thread, missing in Python 3.8
        # (Windows 7 edition).
        await asyncio.get_running_loop().run_in_executor(None, _preparer_vignettes)
        # Summary line, all sources combined.
        print(msg("nouveaux_clips", n=neufs_total))
        runtime.marquer("download")
        if neufs_total:
            # The verb that brings you back is the one that announces: « watch »
            # watches cameras, it should not talk about clips.
            # Follows page language (runtime.lire_langue(), see tray.py): a
            # system notification stays visible with the window closed; it must
            # speak the language the user chose.
            cle = "notif_corps_singulier" if neufs_total == 1 else "notif_corps_pluriel"
            corps = msg(cle, n=neufs_total)
            runtime.ajouter_ligne(
                JOURNAL_TELECHARGEMENTS,
                f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  notification  "
                f"{neufs_total} clip(s) announced",
            )
            # Configured port, not hard-coded 8765: otherwise the notification
            # pointed at the wrong page as soon as the user changed port in
            # settings (review 27/08, bug 5).
            port = runtime.lire_reglages()["port"]
            runtime.toast("Blink", corps, url=f"http://127.0.0.1:{port}/")

    return 1 if had_error else 0
