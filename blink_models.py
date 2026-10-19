"""Models and adapters: local and cloud clips, identity, paths.

Extracted from blink2video.py at step 8 (AUDIT-2026-08-13.md, section 20, 8.2).
This file knows neither the registry (persistence) nor the session (auth):
it transforms blinkpy and cloud objects into a common shape that the
rest of the program handles without caring about the source."""

from __future__ import annotations

import bisect
import collections
import contextlib
import datetime as dt
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

import runtime

# O-06/8.7/8.8: blink_registre.py imports this module for its correlation
# functions only, including for verbs (stop, open) that need neither
# blinkpy nor aiohttp. `Blink` is used here only as a type annotation
# (never instantiated): deferring it under TYPE_CHECKING, combined with
# `from __future__ import annotations`, avoids requiring blinkpy installed
# just to import this file.
if TYPE_CHECKING:
    from blinkpy.blinkpy import Blink

# I-15: reuses the same ftyp box check as merge_daily rather than
# writing a second copy that would drift sooner or later.
import merge_daily as md

LIBELLES = {
    "en": {
        "stockage_non_active": "local storage is not enabled on this hub",
        "stockage_non_actif":
            "local storage is not active (USB drive or microSD card "
            "missing/not recognized, or clips are stored in the cloud)",
        "lecture_manifeste_local": "  Reading the local storage manifest...",
        "module_occupe_attente":
            "  Module busy, retrying in {delay}s ({attempt}/{total})...",
        "manifeste_local_absent":
            "Blink did not return the local storage manifest after "
            "{tentatives} attempts (module stayed busy)",
        "clips_nom_inconnu":
            "  ! {nombre} local storage clip(s) ignored: camera name "
            "unknown to blink2video: {noms}.",
        "jours_negatifs": "the number of days must be zero or positive",
        "cloud_plafond_securite":
            "  ! [data] Cloud manifest close to the safety cap "
            "({plafond} pages): older clips in the requested window may "
            "have been omitted.",
        "cloud_racine_inattendue":
            "  ! [data] Cloud manifest ignored: unexpected JSON root.",
        "cloud_entrees_invalides":
            "  ! [data] {invalides} invalid cloud entrie(s) ignored.",
        "registre_entrees_invalides":
            "  ! [data] {ignores} invalid registry entrie(s) ignored.",
        "resume_volume": "  {n} clip(s), reported size: about {volume}",
        "resume_periode": "  Period: {debut} -> {fin}",
        "resume_cameras": "  Camera(s): {cameras}",
        "aucun": "none",
        "sync_module_introuvable":
            "Sync Module not found: {demande!r}. Available: {disponibles}.",
        "cat_donnees": "data",
        "cat_reseau": "network",
        "cat_http": "HTTP",
        "cat_ecriture": "write",
        "cat_media": "media",
        "contenu_indisponible": "unavailable content",
        "media_absent": "missing media address",
        "aucune_reponse": "no response from the Blink service",
        "statut_http": "HTTP status {statut}",
        "statut_http_absent": "no HTTP status",
        "reponse_refusee": "response refused ({detail})",
        "reponse_vide": "empty response",
        "reponse_tronquee": "truncated response ({recus} bytes received out of {attendu})",
        "mp4_invalide": "body received, but MP4 incomplete or invalid",
        "unite_octet": "B",
        "unite_kibioctet": "KiB",
        "unite_mebioctet": "MiB",
        "unite_gibioctet": "GiB",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])



def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


def _premiere_valeur(*valeurs):
    """First present API value, without treating ``False`` as absent."""
    return next((valeur for valeur in valeurs if valeur not in (None, "")), None)


def _booleen_api(valeur) -> bool:
    """Normalize JSON booleans and their rare textual forms."""
    if isinstance(valeur, str):
        return valeur.casefold().strip() in {
            "1", "true", "yes", "active", "enabled",
        }
    return bool(valeur)


def _objet_emballe(entree, cle: str) -> tuple[dict, dict]:
    """Return the homescreen object and its optional wrapper."""
    enveloppe = entree if isinstance(entree, dict) else {}
    objet = enveloppe.get(cle)
    return (objet if isinstance(objet, dict) else enveloppe), enveloppe


def _noms_reseaux(homescreen: dict) -> dict[str, str]:
    noms = {}
    for entree in homescreen.get("networks") or []:
        reseau, enveloppe = _objet_emballe(entree, "network")
        identifiant = _premiere_valeur(
            reseau.get("id"), reseau.get("network_id"),
            enveloppe.get("id"), enveloppe.get("network_id"),
        )
        nom = _premiere_valeur(reseau.get("name"), enveloppe.get("name"))
        if identifiant is not None and nom is not None:
            noms[str(identifiant)] = str(nom).strip()
    return noms


def _noms_cameras(homescreen: dict, network_id) -> list[str]:
    """Network camera names required by blinkpy's manifest parser."""
    noms = []
    for groupe in ("cameras", "owls", "doorbells"):
        for entree in homescreen.get(groupe) or []:
            appareil, enveloppe = _objet_emballe(entree, "device")
            reseau = _premiere_valeur(
                appareil.get("network_id"), appareil.get("network"),
                enveloppe.get("network_id"), enveloppe.get("network"),
            )
            nom = _premiere_valeur(appareil.get("name"), enveloppe.get("name"))
            if nom is not None and str(reseau) == str(network_id):
                noms.append(str(nom).strip())
    return noms


def _modules_homescreen(blink: Blink) -> list:
    """Adapt real Sync Modules advertised by the homescreen.

    blinkpy 0.25.9 still discovers networks and modules via two legacy
    endpoints. A standalone device (Mini/Doorbell) may then occupy
    ``blink.sync`` with its own ID, while the actual storage module is
    present in ``homescreen["sync_modules"]``. The manifest requires the
    latter's ID. We therefore build the same ``BlinkSyncModule`` surface
    from the response already received, with no extra network call.
    """
    homescreen = getattr(blink, "homescreen", None)
    if not isinstance(homescreen, dict):
        return []
    entrees = homescreen.get("sync_modules")
    if not isinstance(entrees, (list, tuple)):
        return []

    existants = list((getattr(blink, "sync", None) or {}).items())
    noms_reseaux = _noms_reseaux(homescreen)
    modules = []
    identites_vues = set()

    for entree in entrees:
        module, enveloppe = _objet_emballe(entree, "sync_module")
        sync_id = _premiere_valeur(
            module.get("id"), module.get("sync_module_id"),
            enveloppe.get("id"), enveloppe.get("sync_module_id"),
        )
        network_id = _premiere_valeur(
            module.get("network_id"), module.get("network"),
            enveloppe.get("network_id"), enveloppe.get("network"),
        )
        identite = (str(network_id), str(sync_id))
        if sync_id is None or network_id is None or identite in identites_vues:
            continue
        identites_vues.add(identite)

        nom_module = _premiere_valeur(module.get("name"), enveloppe.get("name"))
        nom = noms_reseaux.get(str(network_id)) or str(
            nom_module or network_id
        ).strip()

        # Reuse blinkpy's normal object when it already carries the right ID.
        # An object without an ID may also be the BlinkSyncModule created before
        # the legacy endpoint failed. Conversely, a different ID on the same
        # network belongs to a Mini/Doorbell: never turn it into an XR.
        sync = next(
            (objet for _cle, objet in existants
             if str(getattr(objet, "network_id", "")) == str(network_id)
             and str(getattr(objet, "sync_id", "")) == str(sync_id)),
            None,
        )
        if sync is None:
            sync = next(
                (objet for _cle, objet in existants
                 if str(getattr(objet, "network_id", "")) == str(network_id)
                 and getattr(objet, "sync_id", None) in (None, "")),
                None,
            )
        if sync is None:
            # Lazy import: offline commands (stop/open/merge) must still
            # import blink_models without requiring blinkpy.
            from blinkpy.sync_module import BlinkSyncModule

            sync = BlinkSyncModule(blink, nom, str(network_id), [])

        sync.sync_id = sync_id
        sync.network_id = str(network_id)
        sync.name = nom
        sync.summary = module
        sync.serial = _premiere_valeur(
            module.get("serial"), enveloppe.get("serial"),
            getattr(sync, "serial", None),
        )
        sync.status = _premiere_valeur(
            module.get("status"), enveloppe.get("status"),
            getattr(sync, "status", None),
        )
        sync._version = _premiere_valeur(
            module.get("fw_version"), enveloppe.get("fw_version"),
            getattr(sync, "_version", None),
        )

        stockage = sync._local_storage
        enabled = _premiere_valeur(
            module.get("local_storage_enabled"),
            enveloppe.get("local_storage_enabled"),
        )
        compatible = _premiere_valeur(
            module.get("local_storage_compatible"),
            enveloppe.get("local_storage_compatible"),
        )
        status = _premiere_valeur(
            module.get("local_storage_status"),
            enveloppe.get("local_storage_status"),
        )
        if enabled is not None:
            stockage["enabled"] = _booleen_api(enabled)
        if status is not None:
            stockage["status"] = _booleen_api(status)
        if compatible is not None:
            stockage["compatible"] = _booleen_api(compatible)
        elif stockage.get("enabled") or stockage.get("status"):
            # Storage declared active establishes compatibility even if the
            # API response omitted the redundant ``compatible`` field.
            stockage["compatible"] = True

        from blinkpy.helpers.util import to_alphanumeric

        camera_names = set(_noms_cameras(homescreen, network_id))
        # Fallbacks for homescreen variants that omit a group but blinkpy
        # already instantiated via another route.
        for camera_name, camera in (getattr(blink, "cameras", None) or {}).items():
            camera_network = getattr(camera, "network_id", None)
            if camera_network is None:
                camera_network = getattr(
                    getattr(camera, "sync", None), "network_id", None,
                )
            if str(camera_network) == str(network_id):
                camera_names.add(str(camera_name).strip())
        for _existing_name, existing in existants:
            if str(getattr(existing, "network_id", "")) != str(network_id):
                continue
            camera_names.update(
                str(camera_name).strip()
                for camera_name in (getattr(existing, "cameras", None) or {})
            )

        for camera_name in camera_names:
            sync._names_table[to_alphanumeric(camera_name)] = camera_name

        alias = {nom}
        if nom_module is not None:
            alias.add(str(nom_module).strip())
        sync._blink2video_hub_names = alias
        modules.append((nom, sync))

    return modules


def select_sync_modules(blink: Blink, requested_name: str | None):
    """Select all hubs, or the one requested by name."""
    # The homescreen is the modern inventory and holds the hardware ID required
    # by the local storage API. Fall back to blink.sync for older responses
    # and to preserve historical test/client behavior.
    modules = _modules_homescreen(blink) or list(
        (getattr(blink, "sync", None) or {}).items()
    )
    if not requested_name:
        return modules

    demande = requested_name.casefold().strip()
    selected = [
        (name, sync)
        for name, sync in modules
        if demande in {
            str(alias).casefold().strip()
            for alias in getattr(sync, "_blink2video_hub_names", {name})
        }
    ]
    if not selected:
        available = ", ".join(name for name, _ in modules) or msg("aucun")
        raise ValueError(msg("sync_module_introuvable", demande=requested_name,
                             disponibles=available))
    return selected


# The Sync Module writes each camera name in ASCII in its manifest and on the
# USB key: « 1-Haustür » becomes « 1Haustr » (issue #55, filenames from a
# real key). blinkpy looks up that name in a table built with
# ``re.sub(r"\W+", "", nom)``, where Python 3's ``\W`` keeps accented letters
# (« 1Haustür »): no match, and the clip is dropped with no log line. Cameras
# whose names have accents or umlauts (« Séjour », « Bébé », « Küche ») never
# saw their USB clips.
_TRANSLITTERATION = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe", "ø": "o", "Ø": "O",
})


def _formes_nom_manifeste(nom: str) -> set:
    """ASCII forms under which the Sync Module may write this name.

    Exact module rule unknown (only observed behavior is dropping « ü »): keep
    three plausible transforms—letter removed, de-accented letter (NFKD), and
    German transliteration."""
    formes = set()
    for variante in (
        nom,
        unicodedata.normalize("NFKD", nom),
        nom.translate(_TRANSLITTERATION),
    ):
        # « _ » remains: blinkpy's ``\W`` keeps it too, as for any name
        # already ASCII (the original key is unchanged).
        forme = re.sub(r"[^A-Za-z0-9_]+", "", variante)
        if forme:
            formes.add(forme)
    return formes


def _completer_table_noms(sync) -> None:
    """Add ASCII forms of camera names to blinkpy's table.

    A form that would refer to two different cameras is never added: better an
    unknown clip than one assigned to the wrong camera (its name is the folder).
    An existing key is never overwritten."""
    table = sync._names_table
    candidats = collections.defaultdict(set)
    for nom in {str(propre).strip() for propre in table.values()}:
        for forme in _formes_nom_manifeste(nom):
            candidats[forme].add(nom)
    for forme, noms in candidats.items():
        if len(noms) == 1 and forme not in table:
            table[forme] = next(iter(noms))


async def read_local_manifest(sync) -> list:
    """Ask the Sync Module for its up-to-date local clip list."""
    import asyncio

    storage = sync._local_storage  # blinkpy does not yet expose a public accessor.
    # A real XR Sync Module with active microSD reports
    # ``compatible=false, enabled=true, status=active``. The first flag therefore
    # describes historical compatibility (especially USB), not operational
    # ability to read the XR manifest. blinkpy itself only blocks on
    # ``status``: active storage is the strongest signal and must win.
    if not sync.local_storage and not storage.get("enabled"):
        raise RuntimeError(msg("stockage_non_active"))
    if not sync.local_storage:
        raise RuntimeError(msg("stockage_non_actif"))

    print(msg("lecture_manifeste_local"))
    # The Sync Module handles one command at a time and answers « System is
    # busy » (code 307) until the previous one finishes: a live view that just
    # closed, or another manifest request, is enough. This refusal is temporary,
    # not a failure, hence retry with increasing waits rather than giving up.
    delays = (3, 8, 15, 25)
    _completer_table_noms(sync)
    inconnus = collections.Counter()
    # blinkpy silently drops clips whose camera name is not in its table: we
    # collect those names while parsing the raw response to report them. Instance
    # attribute set then removed; the class stays untouched.
    avait = "poll_local_storage_manifest" in vars(sync)
    original = sync.poll_local_storage_manifest

    async def sonder(*args, **kwargs):
        reponse = await original(*args, **kwargs)
        try:
            for clip in reponse.get("clips") or []:
                nom = clip.get("camera_name")
                if nom not in sync._names_table:
                    inconnus[str(nom)] += 1
        except (AttributeError, TypeError):
            pass
        return reponse

    sync.poll_local_storage_manifest = sonder
    try:
        for attempt, delay in enumerate((*delays, None), start=1):
            inconnus.clear()
            if await sync.update_local_storage_manifest():
                if inconnus:
                    print(msg("clips_nom_inconnu", nombre=sum(inconnus.values()),
                              noms=", ".join(f"« {n} »" for n in sorted(inconnus))))
                return list(storage["manifest"])
            if delay is None:
                break
            print(msg("module_occupe_attente", delay=delay, attempt=attempt,
                      total=len(delays)))
            await asyncio.sleep(delay)
    finally:
        if avait:
            sync.poll_local_storage_manifest = original
        else:
            del sync.poll_local_storage_manifest

    raise RuntimeError(msg("manifeste_local_absent", tentatives=len(delays) + 1))


class CloudClip:
    """A cloud clip, presented like local storage ones.

    Same surface as blinkpy objects: camera name, timestamp, identifier. The
    rest of the pipeline—identity, filename, registry, normalization—therefore
    ignores where the recording came from; that is the point: one archive,
    regardless of source."""

    def __init__(self, entree: dict):
        self.raw = entree
        self.name = str(entree.get("device_name") or "").strip() or "camera"
        self.id = entree.get("id")
        self.created_at = dt.datetime.fromisoformat(str(entree["created_at"]))
        self.address = entree.get("media")
        self.network_id = entree.get("network_id")
        self.device_id = (
            entree.get("device_id") or entree.get("camera_id")
        )
        self.download_issue = None

    async def download_to(self, blink: Blink, target: Path) -> bool:
        """Stream the media to the temporary file chosen by the caller."""
        self.download_issue = None
        target.unlink(missing_ok=True)
        if not self.address:
            self.download_issue = (msg("cat_donnees"), msg("media_absent"))
            return False
        try:
            reponse = await blink.do_http_get(self.address)
            if reponse is None:
                self.download_issue = (msg("cat_reseau"), msg("aucune_reponse"))
                return False
            statut = getattr(reponse, "status", None)
            if not isinstance(statut, int) or not 200 <= statut < 300:
                detail = (msg("statut_http", statut=statut) if isinstance(statut, int)
                          else msg("statut_http_absent"))
                self.download_issue = (msg("cat_http"), msg("reponse_refusee", detail=detail))
                with contextlib.suppress(Exception):
                    await reponse.read()
                return False

            target.parent.mkdir(parents=True, exist_ok=True)
            recus = 0
            flux = getattr(reponse, "content", None)
            iter_chunked = getattr(flux, "iter_chunked", None)
            if callable(iter_chunked):
                # aiohttp may decompress on the fly; chunk size bounds memory
                # without assuming Content-Length.
                with target.open("wb") as destination:
                    async for bloc in iter_chunked(256 * 1024):
                        if bloc:
                            destination.write(bloc)
                            recus += len(bloc)
            else:
                # Compatibility with minimal blinkpy responses and test doubles
                # that only expose ``read()``.
                contenu = await reponse.read()
                with target.open("wb") as destination:
                    destination.write(contenu)
                recus = len(contenu)

            if recus == 0:
                self.download_issue = (msg("cat_http"), msg("reponse_vide"))
                return False

            entetes = getattr(reponse, "headers", None)
            encodage = entetes.get("Content-Encoding") if entetes is not None else None
            longueur = entetes.get("Content-Length") if entetes is not None else None
            if not encodage and longueur is not None:
                try:
                    attendu = int(longueur)
                except (TypeError, ValueError):
                    attendu = None
                if attendu is not None and attendu >= 0 and recus != attendu:
                    self.download_issue = (
                        msg("cat_donnees"),
                        msg("reponse_tronquee", recus=recus, attendu=attendu),
                    )
                    return False

            # Cheap structural check here; the caller does deep validation before
            # atomic rename and cleanup.
            if not md.valid_mp4(target):
                self.download_issue = (msg("cat_donnees"), msg("mp4_invalide"))
                return False
            return True
        except Exception as erreur:
            categorie = msg("cat_ecriture") if isinstance(erreur, OSError) else msg("cat_reseau")
            self.download_issue = (categorie, type(erreur).__name__)
            return False
        finally:
            if self.download_issue is not None:
                with contextlib.suppress(OSError):
                    target.unlink(missing_ok=True)

    async def delete_video(self, blink: Blink) -> bool:
        """Delete this clip from the subscription cloud (GitHub issue #1).

        Undocumented blinkpy endpoint (no equivalent cloud-side method),
        identified and verified manually against a real account:
        POST /api/v1/accounts/{account_id}/media/delete with the list of IDs
        to delete. Return code 711 is service confirmation; anything else
        (network error, different code) counts as failure rather than assumed
        success."""
        url = f"{blink.urls.base_url}/api/v1/accounts/{blink.account_id}/media/delete"
        try:
            resultat = await blink.auth.query(
                url=url, headers=blink.auth.header, reqtype="post",
                data=json.dumps({"media_list": [self.id]}),
            )
        except Exception:
            return False
        return isinstance(resultat, dict) and resultat.get("code") == 711


# get_videos_metadata (blinkpy) loops `for page in range(1, stop)` and
# already stops when a page comes back empty—`stop` is therefore only a safety
# cap, not the real pagination. Set to 20 (19 pages, ~25 items each, ~475
# clips), it acted in practice as silent truncation: an active multi-camera
# account can exceed that volume in 30 days, so the oldest clips in the window
# were never seen (code review 0eab463, bug #6). 400 leaves considerable headroom
# (~10,000 clips) for any real use while staying finite rather than removing
# all protection.
PLAFOND_PAGES_CLOUD = 400


async def read_cloud_manifest(blink: Blink, since_days: int | None) -> list:
    """Inventory of clips kept in the Blink subscription cloud.

    Distinct from the local manifest and independent of the Sync Module: the
    account answers, not the module. A subscription may cover only some cameras,
    so the two inventories overlap only partly—hence matching later rather than
    picking a source."""
    jours = 30 if since_days is None else since_days
    if jours < 0:
        raise ValueError(msg("jours_negatifs"))
    depuis = dt.datetime.now() - dt.timedelta(days=jours)
    entrees = await blink.get_videos_metadata(
        since=depuis.strftime("%Y/%m/%d %H:%M:%S"), stop=PLAFOND_PAGES_CLOUD
    )
    if isinstance(entrees, (list, tuple)) and len(entrees) >= (PLAFOND_PAGES_CLOUD - 3) * 20:
        # Account near the cap: from here we cannot tell whether the last page
        # was empty (true end) or the cap cut off still-active pagination—report
        # rather than decide silently.
        print(msg("cloud_plafond_securite", plafond=PLAFOND_PAGES_CLOUD))
    if entrees is None:
        return []
    if not isinstance(entrees, (list, tuple)):
        print(msg("cloud_racine_inattendue"))
        return []
    clips = []
    invalides = 0
    for entree in entrees:
        if not isinstance(entree, dict):
            invalides += 1
            continue
        # Deleted media stays listed, marked as such; partial media is a
        # recording still being written on Blink's side.
        if entree.get("deleted") or entree.get("partial"):
            continue
        if str(entree.get("type") or "video") != "video":
            continue
        try:
            clips.append(CloudClip(entree))
        except (KeyError, TypeError, ValueError):
            invalides += 1
    if invalides:
        print(msg("cloud_entrees_invalides", invalides=invalides))
    return clips


def _identifiant_reseau(clip, sync=None) -> str:
    """Return the immutable network when the API exposes it."""
    valeur = getattr(clip, "network_id", None)
    if valeur in (None, "") and sync is not None:
        valeur = getattr(sync, "network_id", None)
    return str(valeur or "")


def _identifiant_camera(clip) -> str:
    """Return the hardware ID, missing from blinkpy 0.25 USB objects."""
    for attribut in ("device_id", "camera_id"):
        valeur = getattr(clip, attribut, None)
        if valeur not in (None, ""):
            return str(valeur)
    brut = getattr(clip, "raw", None)
    if isinstance(brut, dict):
        for cle in ("device_id", "camera_id"):
            if brut.get(cle) not in (None, ""):
                return str(brut[cle])
    return ""


def _meme_camera(gauche, droite, sync_gauche=None, sync_droite=None) -> bool:
    """Compare two cameras without conflating two explicit networks."""
    reseau_gauche = _identifiant_reseau(gauche, sync_gauche)
    reseau_droite = _identifiant_reseau(droite, sync_droite)
    if reseau_gauche and reseau_droite and reseau_gauche != reseau_droite:
        return False

    camera_gauche = _identifiant_camera(gauche)
    camera_droite = _identifiant_camera(droite)
    if camera_gauche and camera_droite:
        return camera_gauche == camera_droite

    # blinkpy 0.25 exposes neither device_id nor network_id on some USB objects.
    # In that case only, the original API name (never the sanitized path name)
    # remains the best signal to match USB and cloud.
    # .strip(): a USB object may carry stray leading/trailing space
    # (CloudClip.__init__ already strips its side)—without it, that space alone
    # breaks USB/cloud pairing for the same clip.
    return str(gauche.name).strip().casefold() == str(droite.name).strip().casefold()


def _apparier_evenements(locaux: list, distants: list, tolerance: int = 2, *,
                         priorites_locaux=None, compatibles=None) -> list:
    """Return a maximal deterministic ``(local, remote)`` pairing."""
    if tolerance < 0 or not locaux or not distants:
        return []

    # The time index reduces the graph to neighbors within the window. Adjacency
    # lists are then sorted by delta then index so two runs on the same
    # manifests yield the same pairs.
    marge = dt.timedelta(seconds=tolerance)
    locaux_ordonnes = sorted(
        (clip_datetime_utc(local), indice)
        for indice, local in enumerate(locaux)
    )
    instants_locaux = [instant for instant, _ in locaux_ordonnes]
    priorites = priorites_locaux or [0] * len(locaux)
    meme_camera = compatibles or _meme_camera
    voisinages = [[] for _ in distants]
    for indice_distant, distant in enumerate(distants):
        instant_cloud = clip_datetime_utc(distant)
        debut = bisect.bisect_left(instants_locaux, instant_cloud - marge)
        fin = bisect.bisect_right(instants_locaux, instant_cloud + marge)
        for position in range(debut, fin):
            instant_local, indice_local = locaux_ordonnes[position]
            local = locaux[indice_local]
            if not meme_camera(local, distant):
                continue
            ecart = abs((instant_local - instant_cloud).total_seconds())
            voisinages[indice_distant].append(
                (priorites[indice_local], ecart, indice_local)
            )
        voisinages[indice_distant].sort()

    infini = len(locaux) + len(distants) + 1
    ordre_distants = sorted(
        range(len(distants)),
        key=lambda indice: (
            voisinages[indice][0][:2] if voisinages[indice]
            else (math.inf, math.inf),
            indice,
        ),
    )
    paire_distante = [-1] * len(distants)
    paire_locale = [-1] * len(locaux)

    def niveaux():
        distances = [infini] * len(distants)
        file = []
        for indice_distant in ordre_distants:
            if paire_distante[indice_distant] < 0:
                distances[indice_distant] = 0
                file.append(indice_distant)

        profondeur_libre = infini
        position = 0
        while position < len(file):
            indice_distant = file[position]
            position += 1
            profondeur_suivante = distances[indice_distant] + 1
            if profondeur_suivante > profondeur_libre:
                continue
            for _, _, indice_local in voisinages[indice_distant]:
                autre_distant = paire_locale[indice_local]
                if autre_distant < 0:
                    profondeur_libre = min(profondeur_libre, profondeur_suivante)
                elif (
                    profondeur_suivante < profondeur_libre
                    and distances[autre_distant] == infini
                ):
                    distances[autre_distant] = profondeur_suivante
                    file.append(autre_distant)
        return distances, profondeur_libre

    def augmenter(racine, distances, profondeur_libre, positions):
        # Iterative Hopcroft-Karp traversal: no risk of exceeding recursion
        # depth on a large registry.
        pile = [racine]
        parents = {}
        while pile:
            indice_distant = pile[-1]
            avance = False
            while positions[indice_distant] < len(voisinages[indice_distant]):
                _, _, indice_local = voisinages[indice_distant][
                    positions[indice_distant]
                ]
                positions[indice_distant] += 1
                autre_distant = paire_locale[indice_local]
                if autre_distant < 0:
                    if distances[indice_distant] + 1 != profondeur_libre:
                        continue
                    courant, local_libre = indice_distant, indice_local
                    while True:
                        paire_distante[courant] = local_libre
                        paire_locale[local_libre] = courant
                        if courant == racine:
                            return True
                        courant, local_libre = parents[courant]
                elif (
                    distances[autre_distant] == distances[indice_distant] + 1
                    and autre_distant not in parents
                ):
                    parents[autre_distant] = (indice_distant, indice_local)
                    pile.append(autre_distant)
                    avance = True
                    break
            if not avance:
                distances[indice_distant] = infini
                pile.pop()
        return False

    while True:
        distances, profondeur_libre = niveaux()
        if profondeur_libre == infini:
            break
        positions = [0] * len(distants)
        for indice_distant in ordre_distants:
            if paire_distante[indice_distant] < 0:
                augmenter(indice_distant, distances, profondeur_libre, positions)

    return sorted(
        (indice_local, indice_distant)
        for indice_distant, indice_local in enumerate(paire_distante)
        if indice_local >= 0
    )


def rapprocher(locaux: list, cloud: list, tolerance: int = 2,
               compatibles=None) -> tuple:
    """Split novel cloud clips from those already available locally.

    The same detection may be written on both sides when the subscription
    covers a camera whose local storage also works. Identity is still camera
    and instant, but both sources timestamp the event separately: a few seconds
    apart is possible, hence tolerance. Without it, the same recording would
    be fetched twice and appear duplicated in the daily merge.

    Pure function, covered by dedicated tests without a Blink account."""
    inedits, doublons, _ = rapprocher_paires(locaux, cloud, tolerance, compatibles)
    return inedits, doublons


def rapprocher_paires(locaux: list, cloud: list, tolerance: int = 2,
                      compatibles=None) -> tuple:
    """Like rapprocher(), also returning kept (local index, cloud index) pairs:
    the caller knows which local entries the plan already linked and does not
    reassign them to another clip."""
    paires = _apparier_evenements(locaux, cloud, tolerance, compatibles=compatibles)
    cloud_pris = {indice_cloud for _, indice_cloud in paires}

    doublons = [clip for indice, clip in enumerate(cloud) if indice in cloud_pris]
    inedits = [clip for indice, clip in enumerate(cloud) if indice not in cloud_pris]
    return inedits, doublons, paires


def clip_datetime_utc(clip) -> dt.datetime:
    """Normalize Blink timestamp to UTC."""
    created = clip.created_at
    if created.tzinfo is None:
        return created.replace(tzinfo=dt.timezone.utc)
    return created.astimezone(dt.timezone.utc)


def filter_clips(clips: list, camera: str | None, since_days: int | None) -> list:
    """Apply requested filters, then sort oldest to newest."""
    selected = clips
    if camera:
        # .strip() on both sides: a USB name may carry stray space
        # (see _meme_camera above)—without it, --camera "jardin" would miss
        # a clip whose source name is "jardin " (trailing space), silently.
        cible = camera.strip().casefold()
        selected = [clip for clip in selected if clip.name.strip().casefold() == cible]
    if since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
        selected = [clip for clip in selected if clip_datetime_utc(clip) >= cutoff]
    return sorted(selected, key=clip_datetime_utc)


def reported_bytes(value) -> int:
    """Convert Blink manifest size, expressed in KiB, to bytes."""
    try:
        return int(value) * 1024
    except (TypeError, ValueError):
        return 0


def human_size(size: int) -> str:
    """Display a readable size using the displayed language's units."""
    unites = (msg("unite_octet"), msg("unite_kibioctet"),
              msg("unite_mebioctet"), msg("unite_gibioctet"))
    amount = float(size)
    for unit in unites:
        if amount < 1024 or unit == unites[-1]:
            return f"{amount:.0f} {unit}" if unit == unites[0] else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} {unites[0]}"


# Alias, not a copy: three nearly identical versions of this cleanup had
# drifted (code review 0eab463, bug #5). md.safe_name (originally this one)
# remains the single reference—see its docstring.
safe_name = md.safe_name


def target_path(output: Path, clip, sync=None, source: str | None = None) -> Path:
    """Build a safe path; this path is never business identity.

    The suffix carries a short fingerprint of API values before sanitization.
    Thus two names/IDs that become the same path component, or two hubs reusing
    the same manifest number, stay distinct. ``sync_id`` applies only to USB: in
    the cloud, ``network_id`` denotes the source and the caller's pseudo-module
    must not change the path.
    """
    created = clip_datetime_utc(clip)
    camera = safe_name(clip.name)
    month = created.strftime("%Y-%m")
    identifiant = safe_name(str(clip.id))[:40]
    provenance = source or (
        "cloud" if isinstance(clip, CloudClip) or hasattr(clip, "download_to")
        else "usb"
    )
    empreinte = json.dumps(
        [
            provenance,
            _identifiant_reseau(clip, sync),
            str(getattr(sync, "sync_id", "")) if provenance == "usb" else "",
            str(clip.name),
            str(getattr(clip, "id", "")),
            created.isoformat(),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    suffixe = hashlib.sha256(empreinte.encode("utf-8")).hexdigest()[:12]
    filename = (
        f"{created:%Y-%m-%d_%H-%M-%SZ}_{camera}_{identifiant}_{suffixe}.mp4"
    )
    return output / camera / month / filename


def print_clip_summary(clips: list) -> None:
    """Print a manifest summary, without URLs or sensitive data."""
    total_size = sum(reported_bytes(clip.size) for clip in clips)
    print(msg("resume_volume", n=len(clips), volume=human_size(total_size)))
    if clips:
        first = clip_datetime_utc(clips[0]).astimezone()
        last = clip_datetime_utc(clips[-1]).astimezone()
        print(msg("resume_periode", debut=f"{first:%Y-%m-%d %H:%M:%S %Z}",
                  fin=f"{last:%Y-%m-%d %H:%M:%S %Z}"))
        cameras = sorted({clip.name for clip in clips}, key=str.casefold)
        print(msg("resume_cameras", cameras=", ".join(cameras)))
