"""Incremental registry of acquired clips: load, merge, identity,
USB/cloud correlation, repair of missing or truncated media.

Extracted from blink2video.py at step 8 (AUDIT-2026-08-13.md, section 20, 8.3).
Knows neither the Blink session nor how a clip is downloaded: only
how to know it, identify it, and persist it durably."""

from __future__ import annotations  # Python 3.8 (Windows 7 build): "X | None" annotations are evaluated only when strings are written, never at runtime.

import bisect
import datetime as dt
import hashlib
import json
import os
import stat as stat_module
from pathlib import Path

import runtime

from blink_models import (
    _apparier_evenements,
    _identifiant_camera,
    _identifiant_reseau,
    _meme_camera,
    clip_datetime_utc,
    msg,
)

OUTPUT = runtime.dossier_sorties() / "Blink_Clips"
STATE_FILENAME = ".blink_download_state.json"
STATE_V1_BACKUP_FILENAME = ".blink_download_state.v1.backup.json"


def load_download_state(output: Path) -> dict:
    """Load the incremental registry in the destination folder."""
    state_file = output / STATE_FILENAME
    if not state_file.exists():
        return {"version": 1, "clips": {}}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("JSON root must be an object")
        if state.get("version") not in (1, 2) or not isinstance(state.get("clips"), dict):
            raise ValueError("unknown format")
        valides = {}
        ignores = 0
        for cle, entree in state["clips"].items():
            if not isinstance(cle, str) or not isinstance(entree, dict):
                ignores += 1
                continue
            instant = entree.get("created_at")
            if not isinstance(instant, str):
                ignores += 1
                continue
            try:
                dt.datetime.fromisoformat(instant)
            except ValueError:
                ignores += 1
                continue
            # Additive v1 migration: legacy keys and archives stay
            # intact. New metadata enables lookups
            # without relying on filename or renumbered USB ID.
            entree.setdefault("source", "usb")
            entree.setdefault("network_id", "")
            entree.setdefault("device_id", "")
            entree.setdefault("remote_id", "")
            entree.setdefault("sync_id", str(cle).split(":", 1)[0])
            # .strip(): same reason as in state_key() below (USB name
            # with stray space without device_id). Only affects old
            # v1 entries with no camera_identity at all: setdefault never
            # touches an entry that already has one, migrated or not.
            camera_identite = (
                f"device:{entree['device_id']}" if entree["device_id"]
                else f"name:{str(entree.get('camera') or 'camera').strip().casefold()}"
            )
            entree.setdefault("camera_identity", camera_identite)
            empreinte = json.dumps(
                [entree["network_id"], camera_identite, instant],
                ensure_ascii=False, separators=(",", ":"),
            )
            entree.setdefault(
                "correlation_id",
                hashlib.sha256(empreinte.encode("utf-8")).hexdigest(),
            )
            valides[cle] = entree
        if ignores:
            print(msg("registre_entrees_invalides", ignores=ignores))
        state["clips"] = valides
        state["version"] = 2
        return state
    except OSError:
        categorie = "file"
    except json.JSONDecodeError:
        categorie = "JSON data"
    except ValueError:
        categorie = "JSON schema"
    print(
        f"  ! Incremental state unreadable [{categorie}]; "
        "existing files will be checked."
    )
    return {"version": 1, "clips": {}}


def save_download_state(output: Path, state: dict) -> None:
    """Save the incremental registry without overwriting another worker's changes.

    Two loops now write here: one for USB every ten minutes, the other for
    cloud every minute. Writing one's own copy in one shot would drop clips the
    other just added: so the file is re-read just before writing, and one's own
    entries are merged on top. An entry is never removed on this path, only
    added or updated, which makes merging safe.

    Replacement stays atomic: a crash mid-write leaves the old registry intact
    rather than a truncated file."""
    output.mkdir(parents=True, exist_ok=True)
    state_file = output / STATE_FILENAME
    with runtime.verrou("registre", "ecriture", stale_after=60, attente=10):
        _sauvegarder_registre_v1(state_file)
        _ecrire_registre(state_file, state)


def _sauvegarder_registre_v1(state_file: Path) -> None:
    """Keep a single copy of the v1 registry before its first migration."""
    backup = state_file.with_name(STATE_V1_BACKUP_FILENAME)
    if not state_file.exists():
        return
    try:
        brut = state_file.read_bytes()
        ancien = json.loads(brut)
        if not isinstance(ancien, dict) or ancien.get("version") != 1:
            return
        if backup.exists():
            sauvegarde = backup.read_bytes()
            chargee = json.loads(sauvegarde)
            if not isinstance(chargee, dict) or chargee.get("version") != 1:
                raise OSError("invalid v1 backup")
            return
        temporaire = backup.with_suffix(backup.suffix + ".tmp")
        temporaire.write_bytes(brut)
        temporaire.replace(backup)
        if backup.read_bytes() != brut:
            raise OSError("unverifiable v1 backup")
    except (OSError, json.JSONDecodeError) as erreur:
        raise OSError(
            "v2 migration cancelled: cannot back up the v1 registry"
        ) from erreur


def _exclusion_a_retenir(precedente: dict, entree: dict) -> tuple:
    """Exclusion state (excluded, excluded_at) to keep for a clip found
    on both sides during merge.

    The exclude/re-include decision (made by set_excluded(), never
    by the downloader) is separate state: for this field alone, only the
    most recent decision must win, both ways - a fixed priority for
    excluded=True protected an exclusion against a stale downloader copy,
    but let a re-inclusion be undone by that same copy arriving later
    (review 27/08, bug 2).

    A missing excluded_at on one side proves nothing about real order: it
    may be a new entry that does not know this field, or data from before
    it was introduced. A dated side therefore always beats a silent one;
    only when neither is dated do we fall back to historical behavior (B-04):
    never let a silent copy cancel an active exclusion."""
    disque_exclu = bool(precedente.get("excluded"))
    entrant_exclu = bool(entree.get("excluded"))
    depuis_disque = precedente.get("excluded_at")
    depuis_entrant = entree.get("excluded_at")

    # Even when booleans match, the most recent date must
    # survive: rolling it back would let a stale opposite decision
    # win on a later download save.
    if depuis_disque and depuis_entrant:
        if depuis_disque > depuis_entrant:
            return disque_exclu, depuis_disque
        return entrant_exclu, depuis_entrant
    if depuis_disque and not depuis_entrant:
        return disque_exclu, depuis_disque
    if depuis_entrant and not depuis_disque:
        return entrant_exclu, depuis_entrant
    return disque_exclu or entrant_exclu, None


def _ecrire_registre(state_file: Path, state: dict) -> None:
    """Merge one's own entries onto those already on disk, atomically."""
    output = state_file.parent
    fusionne = dict(state)
    disque = load_download_state(output)
    clips = dict(disque.get("clips") or {})
    for cle, entree in (state.get("clips") or {}).items():
        precedente = clips.get(cle)
        if isinstance(precedente, dict) and isinstance(entree, dict):
            excluded, excluded_at = _exclusion_a_retenir(precedente, entree)
            if (excluded, excluded_at) != (bool(entree.get("excluded")), entree.get("excluded_at")):
                entree = dict(entree)
                entree["excluded"] = excluded
                if excluded_at is not None:
                    entree["excluded_at"] = excluded_at
        clips[cle] = entree
    fusionne["clips"] = clips
    fusionne["version"] = max(
        2, int(disque.get("version") or 1), int(state.get("version") or 1),
    )
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(fusionne, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(state_file)
    _invalider_index_registre(state)
    state["version"] = fusionne["version"]
    state["clips"] = fusionne.get("clips", state.get("clips"))


def state_key(sync, clip, source: str = "usb") -> str:
    """Identify provenance without conflating it with correlation.

    A USB identity includes module, network, camera, and instant, but never
    manifest ID: the Sync Module renumbers it on reindex.
    A cloud identity adds the remote media ID instead, immutable
    when provided by the API. USB/cloud matching remains a
    separate operation, tolerant on instant.

    With no ``device_id`` on blinkpy 0.25 USB objects, their original API
    name is the documented fallback; a USB camera rename therefore
    cannot be recognized with certainty."""
    created = clip_datetime_utc(clip).isoformat()
    # .strip() before .casefold(): consistent with camera_setting_key below
    # and CloudClip.__init__ (blink_models.py). A blinkpy 0.25 USB object without
    # device_id may return a name with stray leading/trailing space (seen
    # in production registry, 38 "jardin " clips isolated from
    # the same camera's history between 07 and 11/08); without
    # this trim, that space creates a separate correlation identity.
    camera = _identifiant_camera(clip) or f"name:{str(clip.name).strip().casefold()}"
    identite = json.dumps(
        [
            source,
            str(getattr(sync, "sync_id", "")),
            _identifiant_reseau(clip, sync),
            camera,
            created,
            str(getattr(clip, "id", "")) if source == "cloud" else "",
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "v2:" + hashlib.sha256(identite.encode("utf-8")).hexdigest()


def camera_setting_key(sync, clip) -> str:
    """Stable key for a camera-specific destructive setting."""
    network_id = _identifiant_reseau(clip, sync)
    device_id = _identifiant_camera(clip)
    material = (
        ["device", network_id, device_id]
        if device_id
        else [
            "legacy", network_id, str(getattr(sync, "sync_id", "")),
            str(getattr(clip, "name", "camera")).strip().casefold(),
        ]
    )
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"camera-v2-{digest[:32]}"


def camera_setting_key_from_entry(entry: dict) -> str:
    """Same key from a persisted registry entry."""
    network_id = str(entry.get("network_id") or "")
    device_id = str(entry.get("device_id") or "")
    material = (
        ["device", network_id, device_id]
        if device_id
        else [
            "legacy", network_id, str(entry.get("sync_id") or ""),
            str(entry.get("camera") or "camera").strip().casefold(),
        ]
    )
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"camera-v2-{digest[:32]}"


def remember_download(state: dict, sync, hub_name: str, clip, output: Path,
                      target: Path, source: str = "usb") -> None:
    """Mark a clip as acquired only when its file exists.

    Provenance is recorded: a subscription-covered camera records
    in the cloud, another on the module key. The UI shows this per
    camera, avoiding guesswork about why a given camera produces nothing."""
    _invalider_index_registre(state)
    state["version"] = 2
    state.setdefault("clips", {})[state_key(sync, clip, source)] = {
        "hub": hub_name,
        "camera": clip.name,
        "created_at": clip_datetime_utc(clip).isoformat(),
        "path": target.relative_to(output).as_posix(),
        "bytes": target.stat().st_size,
        "source": source,
        "network_id": _identifiant_reseau(clip, sync),
        "device_id": _identifiant_camera(clip),
        "remote_id": str(getattr(clip, "id", "")),
        "sync_id": str(getattr(sync, "sync_id", "")),
        # .strip(): same reason as in state_key() (USB name with stray
        # space without device_id) - written on every new download,
        # the most important site to protect of the three in this file.
        "camera_identity": (
            f"device:{_identifiant_camera(clip)}" if _identifiant_camera(clip)
            else f"name:{str(clip.name).strip().casefold()}"
        ),
    }
    entree = state["clips"][state_key(sync, clip, source)]
    empreinte = json.dumps(
        [entree["network_id"], entree["camera_identity"], entree["created_at"]],
        ensure_ascii=False, separators=(",", ":"),
    )
    entree["correlation_id"] = hashlib.sha256(empreinte.encode("utf-8")).hexdigest()


def _chemin_entree(output: Path, entry: dict, *, index=None,
                   cle: str | None = None) -> Path | None:
    """Resolve a registry media path only inside ``output``."""
    brut = entry.get("path")
    if not isinstance(brut, str) or not brut.strip():
        return None
    cache_cle = (cle, os.fspath(output))
    if index is not None and cle is not None:
        memorise = index.chemins.get(cache_cle)
        if memorise is not None and memorise[0] == brut:
            return memorise[1]
    relatif = Path(brut)
    if relatif.is_absolute() or ".." in relatif.parts:
        return None
    if index is None:
        racine = output.resolve()
    else:
        racine = index.racines.get(os.fspath(output))
        if racine is None:
            racine = output.resolve()
            index.racines[os.fspath(output)] = racine
    chemin = (racine / relatif).resolve()
    try:
        chemin.relative_to(racine)
    except ValueError:
        return None
    if index is not None and cle is not None:
        index.chemins[cache_cle] = (brut, chemin)
    return chemin


class _ClipConnu:
    """Registry entry presented as a clip, for matching."""

    def __init__(self, entree: dict):
        self.name = entree.get("camera") or "camera"
        self.created_at = dt.datetime.fromisoformat(entree["created_at"])
        self.id = entree.get("remote_id") or 0
        self.device_id = entree.get("device_id") or ""
        self.network_id = entree.get("network_id") or ""
        # Provenance and remote id, for meme_evenement_cloud().
        self.source = entree.get("source") or "usb"
        self.remote_id = str(entree.get("remote_id") or "")


def _identifiants_cloud_distincts(connu, clip) -> bool:
    """True if cloud entry ``connu`` and cloud clip ``clip`` carry two
    different remote ids: then they are two media, regardless
    of time gap (audit 26/09/2026, B01). A USB entry
    keeps manifest number in remote_id, which the Sync Module
    renumbers: it is never comparable to a cloud id. An
    old entry without id is still matched by instant alone."""
    if getattr(connu, "source", "") != "cloud":
        return False
    gauche = str(getattr(connu, "remote_id", "") or "")
    droite = str(getattr(clip, "id", "") or "")
    return bool(gauche and droite and gauche != droite)


def meme_evenement_cloud(connu, clip, sync=None) -> bool:
    """Can a known entry be the same event as this cloud clip?
    Same camera within tolerance, except two different cloud ids."""
    return (_meme_camera(connu, clip, sync_droite=sync)
            and not _identifiants_cloud_distincts(connu, clip))


class _IndexRegistre:
    """Ephemeral temporal view of a registry, never included in its JSON."""

    def __init__(self, state: dict):
        self.state = state
        self.clips = state.get("clips")
        self.taille = len(self.clips) if isinstance(self.clips, dict) else 0
        self.valides = []
        self.racines = {}
        self.chemins = {}
        self.fichiers = {}
        temporels = []
        tombstones = []
        for position, (cle, entry) in enumerate((self.clips or {}).items()):
            if not isinstance(cle, str) or not isinstance(entry, dict):
                continue
            if not entry.get("created_at"):
                continue
            try:
                connu = _ClipConnu(entry)
                instant = clip_datetime_utc(connu)
            except (TypeError, ValueError):
                continue
            self.valides.append((cle, entry, connu, position))
            element = (instant, position, cle, entry, connu)
            temporels.append(element)
            if entry.get("excluded"):
                tombstones.append(element)
        self.temporels = sorted(temporels)
        self.instants = [element[0] for element in self.temporels]
        self.tombstones = sorted(tombstones)
        self.instants_tombstones = [element[0] for element in self.tombstones]


_CACHE_INDEX_REGISTRE = None


def _invalider_index_registre(state: dict) -> None:
    """Drop the ephemeral view before any internal registry mutation."""
    global _CACHE_INDEX_REGISTRE
    if (
        _CACHE_INDEX_REGISTRE is not None
        and _CACHE_INDEX_REGISTRE.state is state
    ):
        _CACHE_INDEX_REGISTRE = None


def _index_registre(state: dict) -> _IndexRegistre:
    """Reuse a single non-JSON index for successive lookups."""
    global _CACHE_INDEX_REGISTRE
    clips = state.get("clips")
    taille = len(clips) if isinstance(clips, dict) else 0
    if (
        _CACHE_INDEX_REGISTRE is None
        or _CACHE_INDEX_REGISTRE.state is not state
        or _CACHE_INDEX_REGISTRE.clips is not clips
        or _CACHE_INDEX_REGISTRE.taille != taille
    ):
        _CACHE_INDEX_REGISTRE = _IndexRegistre(state)
    return _CACHE_INDEX_REGISTRE


def _trouver_entree(state: dict, sync, clip,
                    consumed: set[str] | None = None,
                    index: _IndexRegistre | None = None,
                    source: str = "usb",
                    ) -> tuple[str | None, dict | None]:
    """Find at most one correlated entry, tombstones taking priority."""
    cle_exacte = state_key(sync, clip, source)
    entry = None if cle_exacte in (consumed or ()) else state["clips"].get(cle_exacte)
    if isinstance(entry, dict) and entry.get("excluded"):
        return cle_exacte, entry

    index = index or _index_registre(state)
    instant = clip_datetime_utc(clip)
    marge = dt.timedelta(seconds=2)
    # With an ordinary exact key, only an old tombstone can take
    # priority. Not rescanning ordinary media preserves the fast path.
    if isinstance(entry, dict):
        temporels = index.tombstones
        instants = index.instants_tombstones
    else:
        temporels = index.temporels
        instants = index.instants
    debut = bisect.bisect_left(instants, instant - marge)
    fin = bisect.bisect_right(instants, instant + marge)
    meilleur = None
    for indice in range(debut, fin):
        instant_connu, position, cle, candidat, connu = temporels[indice]
        if cle in (consumed or ()):
            continue
        ecart = abs((instant_connu - instant).total_seconds())
        compatible = (meme_evenement_cloud(connu, clip, sync) if source == "cloud"
                      else _meme_camera(connu, clip, sync_droite=sync))
        if compatible:
            classement = (
                not bool(candidat.get("excluded")),
                cle != cle_exacte,
                ecart,
                position,
            )
            if meilleur is None or classement < meilleur[0]:
                meilleur = (classement, cle, candidat)
    if meilleur is None:
        return (cle_exacte, entry) if isinstance(entry, dict) else (None, None)
    return meilleur[1], meilleur[2]


def _hub_compatible(entry: dict, sync) -> bool:
    """False only if both hubs are known and clearly different.

    _meme_camera() compares network_id, missing from old USB entries
    (blinkpy 0.25 did not expose them): without a second signal, matching
    falls back to camera name alone and would confuse two
    homonymous hubs. Hub name, however, is set from the first
    remember_download() and remains on those same old entries."""
    hub_connu = str(entry.get("hub") or "").strip().casefold()
    hub_courant = str(getattr(sync, "name", "") or "").strip().casefold()
    return not hub_connu or not hub_courant or hub_connu == hub_courant


def _apparier_registre(state: dict, sync, clips: list,
                       index: _IndexRegistre | None = None) -> dict:
    """Batch-match each USB clip to at most one registry entry."""
    index = index or _index_registre(state)
    correspondances = {}
    cles_prises = set()
    clips_pris = set()

    # Exclusion is an absolute user decision, even if an ordinary entry
    # otherwise has an exact source key for the clip.
    tombstones = [
        (cle, entry, connu)
        for cle, entry, connu, _ in index.valides
        if entry.get("excluded") and _hub_compatible(entry, sync)
    ]
    if tombstones:
        paires_exclues = _apparier_evenements(
            [element[2] for element in tombstones],
            clips,
            2,
            compatibles=lambda connu, clip: _meme_camera(
                connu, clip, sync_droite=sync,
            ),
        )
        for indice_entree, indice_clip in paires_exclues:
            cle, entry, _ = tombstones[indice_entree]
            correspondances[indice_clip] = (cle, entry)
            cles_prises.add(cle)
            clips_pris.add(indice_clip)

    # For the rest, exact source identity beats
    # tolerant correlation.
    for indice_clip, clip in enumerate(clips):
        if indice_clip in clips_pris:
            continue
        cle = state_key(sync, clip)
        entry = state.get("clips", {}).get(cle)
        if (
            isinstance(entry, dict)
            and not entry.get("excluded")
            and cle not in cles_prises
        ):
            correspondances[indice_clip] = (cle, entry)
            cles_prises.add(cle)
            clips_pris.add(indice_clip)

    restants = [indice for indice in range(len(clips)) if indice not in clips_pris]

    entrees = [
        (cle, entry, connu)
        for cle, entry, connu, _ in index.valides
        if cle not in cles_prises and not entry.get("excluded")
        and _hub_compatible(entry, sync)
    ]
    if not entrees or not restants:
        return correspondances

    connus = [element[2] for element in entrees]
    clips_restants = [clips[indice] for indice in restants]
    paires = _apparier_evenements(
        connus,
        clips_restants,
        2,
        compatibles=lambda connu, clip: _meme_camera(
            connu, clip, sync_droite=sync,
        ),
    )
    for indice_entree, indice_restant in paires:
        cle, entry, _ = entrees[indice_entree]
        correspondances[restants[indice_restant]] = (cle, entry)
    return correspondances


def is_downloaded(state: dict, sync, clip, target: Path,
                  consumed: set[str] | None = None,
                  index: _IndexRegistre | None = None,
                  source: str = "usb") -> bool:
    """A clip is acquired if the registry and a non-empty file are present.

    Exception: a clip marked excluded counts as acquired even without a file.
    That is a tombstone, set by `merge_daily.py --exclude`, meaning
    deliberately skipped, do not fetch again; without it, deleting the
    file would only trigger a new download. Same idea
    as yt-dlp's archive file (--download-archive, inherited from
    youtube-dl): remember the id, not media presence."""
    index = index or _index_registre(state)
    cle_entree, entry = _trouver_entree(
        state, sync, clip, consumed, index, source,
    )
    if isinstance(entry, dict) and entry.get("excluded"):
        acquis = True
    elif not isinstance(entry, dict):
        acquis = False
    else:
        chemin = _chemin_entree(
            target.parents[2], entry, index=index, cle=cle_entree,
        )
        empreinte_fichier = None
        if chemin is not None:
            try:
                stat = chemin.stat()
                empreinte_fichier = (
                    os.fspath(chemin), stat.st_size, stat.st_mtime_ns,
                    entry.get("bytes"),
                )
            except OSError:
                stat = None
        else:
            stat = None
        memorise = index.fichiers.get(cle_entree)
        if memorise is not None and memorise[0] == empreinte_fichier:
            acquis = memorise[1]
        elif stat is None or not stat_module.S_ISREG(stat.st_mode):
            acquis = False
        else:
            taille = stat.st_size
            annoncee = entry.get("bytes")
            acquis = taille > 0 and (
                not isinstance(annoncee, int) or annoncee <= 0 or taille == annoncee
            )
        if cle_entree is not None:
            index.fichiers[cle_entree] = (empreinte_fichier, acquis)
    if acquis and consumed is not None:
        consumed.add(cle_entree)
    return acquis


def _entree_acquise(output: Path, entree: dict) -> bool:
    """A tombstone stays known; ordinary media must exist and be complete."""
    if not isinstance(entree, dict):
        return False
    if entree.get("excluded"):
        return True
    chemin = _chemin_entree(output, entree)
    if chemin is None or not chemin.is_file():
        return False
    taille = chemin.stat().st_size
    annoncee = entree.get("bytes")
    return taille > 0 and (
        not isinstance(annoncee, int) or annoncee <= 0 or taille == annoncee
    )
