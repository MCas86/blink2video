#!/usr/bin/env python3
"""Update from GitHub releases.

Two stages, separated because they do not carry the same risk.

The first costs nothing and commits to nothing: ask GitHub what the
latest published version is, compare it to ours, keep the answer in cache.
That is what lights up the indicator in the interface.

The second replaces the installation. The order is dictated by a simple
constraint: a program cannot replace itself while it is running,
and under Windows it cannot even be moved. So we download the archive,
extract it into the installation's ``update`` subfolder, verify that
the new executable responds, and it is *that* one we charge with finishing the work.
Launched from this preparation folder, outside the three replaced elements, it
can stop the old version, swap the files, then relaunch. Nothing
is touched until the new version has proven it starts.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import runtime
from blink_tls import contexte_tls

try:
    from nico579_commons import maj_install
except ImportError:  # this module must stay importable without dependencies (python -S)
    maj_install = None

LIBELLES = {
    "en": {
        "aide_desc": "Install the latest published release.",
        "aide_check": "say whether a newer release exists, without installing anything",
        "message_windows7":
            "Automatic updates disabled for the legacy Windows 7 "
            "edition: a standard Windows archive would reinstall Python 3.12 "
            "and no longer start on this system.",
        "empreinte_release_absente":
            "This release provides no SHA-256 checksum: automatic update "
            "refused. Download it manually from GitHub.",
        "archive_recue": "  archive received: {nom} ({mo} MB)",
        "version_dossier_impropre": "Version number improper for a folder: {version!r}",
        "dossier_deja_existant":
            "The folder {racine} already exists and does not belong to "
            "blink2video. Rename it before running the update again.",
        "echec_binaire_absent": "Failed: {nom} missing from the archive.",
        "echec_binaire_ne_demarre_pas": "Failed: the new executable does not start ({erreur}).",
        "echec_binaire_annonce":
            "Failed: the new executable reports « {annonce} », "
            "expected « {attendue} ».",
        "binaire_verifie": "  verified: {annonce}",
        "relance": "Relaunching: {commande}",
        "hors_du_service":
            "Under the systemd service: continuing outside the unit, "
            "to survive its stop.",
        "racine_controle_illisible": "Update interrupted: control root unreadable.",
        "racines_controle_multiples":
            "Update interrupted: multiple possible control roots.",
        "pas_un_depot_git":
            "These sources don't come from a git repository: nothing to pull. "
            "Download the published archive, or clone the repository.",
        "deja_a_jour": "blink2video {version} is up to date.",
        "maj_commun": "Updating with the common installer.",
        "maj_ancien_chemin": "Common installer not possible here ({raison}): updating file by file.",
        "echec_maj_commun": "The update failed: {erreur}",
        "maj_git_pull": "Updating {ancienne} to {neuve} (git pull)",
        "git_pull_refuse":
            "« git pull » refused: local changes may be pending. "
            "Nothing has changed.",
        "depot_version_inattendue":
            "The repository reports {obtenue} after pulling, expected {neuve}. "
            "Relaunch refused.",
        "passage_nouvelle_version": "Switching to the new version…",
        "archive_absente_pour_ce_systeme":
            "Version {version} is published, but with no archive "
            "for this system. See {page}",
        "maj_vers": "Updating {ancienne} to {neuve}",
        "echec_maj_binaire_incorrect":
            "Update failed: the new executable did not start correctly.",
        "echec_maj_exception": "Update failed: {type}: {erreur}",
        "version_disponible": "Version {version} available (you have {actuelle}): {page}",
    },
}
LIBELLES["fr"] = dict(LIBELLES["en"])


# Permutation messages (stop, replace, restore) come from the
# common package: one text for the four applications.
if maj_install is not None:
    for _langue, _textes in maj_install.LIBELLES_PERMUTATION.items():
        LIBELLES[_langue].update(_textes)

LIBELLES["fr"] = dict(LIBELLES["en"])


def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


DEPOT = "nico579/blink2video"
CACHE = Path(".blink_maj.json")
# One hour. Six hours made a published version wait up to half a
# day (issue #35); one query per hour stays far below the sixty
# the GitHub API grants per hour per address without an account. To not
# wait at all, the « Check for updates » button in settings
# (verifier_maintenant). The interface itself reads the cache without ever
# querying GitHub when a page opens.
FRAICHEUR = 3600
DOSSIER_TRAVAIL = "update"
MARQUEUR_TRAVAIL = ".blink2video-update"
# Before 0.10.5, updates were prepared next to the installation.
# Keeping this prefix lets us erase any leftovers one last time.
PREFIXE_TRAVAIL_HISTORIQUE = ".blink_maj_"
# An official archive is about 120 MB today. These ceilings do not
# guess its size (the one published by GitHub must match
# exactly), but refuse before writing a plainly absurd metadata
# value and bound a hostile compressed archive.
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000


# ------------------------------------------------------------------ detection

def _archive_de_ce_systeme(assets: list) -> dict:
    """The published archive that matches this machine, if any."""
    if sys.platform == "win32":
        marque = "windows"
    elif sys.platform == "darwin":
        marque = "macos"
    else:
        marque = "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    par_nom = {str(asset.get("name") or ""): asset for asset in assets
               if isinstance(asset, dict)}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        nom = str(asset.get("name", ""))
        nom_minuscule = nom.lower()
        # The Windows 7 artefact stays manual. Even if wrongly added to
        # a release, a recent Windows must not pick it instead of
        # the official archive, which also carries « windows » in its name.
        if marque == "windows" and "windows7" in nom_minuscule:
            continue
        suffixe = ".zip" if marque in ("windows", "macos") else ".tar.gz"
        if (marque in nom_minuscule and arch in nom_minuscule
                and nom_minuscule.endswith(suffixe)):
            checksum = par_nom.get(nom + ".sha256") or {}
            return {
                "nom": nom,
                "url": asset.get("browser_download_url"),
                "taille": int(asset.get("size") or 0),
                # GitHub now exposes the asset digest. The companion
                # file remains a fallback for API responses that would
                # not yet provide this field.
                "sha256": _archive().sha256_normalise(asset.get("digest")),
                "checksum_url": checksum.get("browser_download_url"),
            }
    return {}


# Looking up the latest release stays HERE and not in
# nico579_commons.maj (Verificateur), unlike the other three
# applications: this module is the update program, it must import
# and work with NO installed dependency (it is precisely when
# dependencies are broken that we need it; test_updater_reste_importable_
# sans_dependances, python -S). And installer() needs this system's
# archive and hash check, which Verificateur does not carry.
# Assumed specialization, seen comparing the twins on 2026-10-06.
def _ouvrir_github(url: str) -> dict:
    """GitHub's JSON response, with contexte_tls() roots (system and
    certifi: Windows 7 no longer receives all new ones)."""
    requete = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": f"blink2video/{runtime.VERSION}"})
    with urllib.request.urlopen(requete, timeout=10,
                                context=contexte_tls()) as reponse:
        return json.loads(reponse.read().decode("utf-8"))


@functools.lru_cache(maxsize=None)
def _verificateur():
    """Latest-release lookup is that of nico579_commons.maj
    (Verificateur), the same for the four applications; here, the TLS context
    and the on-disk cache in the application folder. Imported on use: this
    module is the update program, it must import without any
    dependency (test_updater_reste_importable_sans_dependances)."""
    from nico579_commons import maj as maj_commune
    return maj_commune.Verificateur(
        DEPOT, runtime.VERSION, fraicheur_s=FRAICHEUR, ouvrir=_ouvrir_github,
        cache=runtime.app_dir() / CACHE)


def _version_locale() -> str:
    """The VERSION actually on disk, for whoever runs from sources.

    A bundle cannot diverge from itself mid-flight (see
    `frozen()`, only the update replaces its files, and it always
    restarts the process): `runtime.VERSION` then suffices. From
    sources, this file can change under an already-running process (pull or
    commit while blink2video runs); comparing to the constant loaded
    at import would make an already-installed version look still upcoming, and
    the update triggered from that false positive would find nothing
    left to do (observed in the wild: « Install » button that never
    does anything)."""
    if runtime.frozen():
        return runtime.VERSION
    try:
        chemin = Path(__file__).resolve().parent / "runtime.py"
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            if ligne.startswith("VERSION = "):
                return ligne.split('"')[1]
    except OSError:
        pass
    return runtime.VERSION


def _conclure_sans_relance(message: str) -> None:
    """Publishes an update conclusion that neither stops nor relaunches anything.

    `installer()` and `_depuis_les_sources()` share a constraint: neither an
    « already up to date », nor a missing archive, nor a refused `git pull` will
    stop then relaunch the server. The web page nevertheless expects precisely
    this disappear-then-return to know it is done (see its
    comment « the server will disappear then come back »). Without this signal,
    the button stays stuck on « Updating… » indefinitely, with neither
    error nor « already up to date » ever surfacing (observed in the wild: click with
    no visible effect). `total=1`: a single step, not a progress measure,
    so no fraction shown (montrerTravail only shows N/N if
    total > 1)."""
    print(message)
    runtime.travail(message, 1, 1, cle="phase.update_noop")
    runtime.fin_travail(conserver=runtime.TRAVAIL_TERMINE_VISIBLE)


def disponible(force: bool = False, reseau: bool = True) -> dict:
    """The published version if it is newer than ours, otherwise nothing.

    The cache avoids calling GitHub on every question, and still helps when the
    machine is offline: an update signaled yesterday remains true. Without
    `reseau`, we settle for the last answer: that is how
    the interface responds, a page request not having to wait for GitHub."""
    if runtime.build_windows7():
        return {}
    verificateur = _verificateur()
    # From sources, the file can change under a running process.
    verificateur.version_locale = _version_locale()
    verifie = verificateur.verifie_a
    if reseau and (force or verifie is None or time.time() - verifie > FRAICHEUR):
        verificateur.verifier()
    trouvee = verificateur.disponible()
    if not trouvee:
        return {}
    return {"version": trouvee["version"], "page": trouvee["page"],
            "archive": _archive_de_ce_systeme(trouvee["assets"])}


def verifier_maintenant() -> tuple:
    """For the « Check for updates » button in settings: queries
    GitHub without waiting for the cache to age (issue #35). Returns the newer
    version, or {}, and whether GitHub replied: the page thus distinguishes
    « already up to date » from « GitHub unreachable », which disponible()
    deliberately conflates for the background thread. The Windows 7 edition offers none."""
    if runtime.build_windows7():
        return {}, True
    repondu = _verificateur().verifier()
    return disponible(reseau=False), repondu


# --------------------------------------------------------------- installation

def _archive():
    """nico579_commons.maj_archive: release file choice and validation,
    verified download, safe extraction. Imported on use, not at
    import: this module must stay importable without any dependency
    (test_updater_reste_importable_sans_dependances)."""
    from nico579_commons import maj_archive
    return maj_archive


@contextlib.contextmanager
def _en_langue_courante():
    """Common-package refusals carry a key and its values; here they become
    OSError again with the page language text, as before."""
    try:
        yield
    except _archive().ErreurMiseAJour as erreur:
        raise OSError(erreur.message(runtime.lire_langue())) from erreur


def _agent() -> str:
    return f"blink2video/{runtime.VERSION}"


def _url_release_officielle(url: str, nom: str) -> bool:
    """Ties the initial URL to the expected repository, tag format and file: the
    update cache lives in the data folder; its contents are not
    an authority."""
    return _archive().url_de_release(url, DEPOT, nom)


def _lire_empreinte(url: str, nom_archive: str) -> str:
    """Reads the small ``<archive>.sha256`` file published with the archive."""
    with _en_langue_courante():
        return _archive().lire_empreinte(
            url, nom_archive, DEPOT, agent=_agent(), ouvrir=urllib.request.urlopen,
            contexte=contexte_tls())


def _empreinte_attendue(archive: dict) -> str:
    empreinte = _archive().sha256_normalise(archive.get("sha256"))
    if empreinte:
        return empreinte
    checksum_url = str(archive.get("checksum_url") or "")
    if checksum_url:
        return _lire_empreinte(checksum_url, str(archive.get("nom") or ""))
    raise OSError(msg("empreinte_release_absente"))


def _nom_archive_sur(nom) -> str:
    with _en_langue_courante():
        return _archive().nom_archive_sur(nom)


def _telecharger(url: str, destination: Path, taille: int, sha256: str) -> None:
    """Fetches the archive while publishing its progress.

    The same channel as clip download and assembly: the interface
    already shows this bar, there was nothing to invent."""
    def progression(recu: int, total: int) -> None:
        runtime.travail(
            f"Téléchargement de la mise à jour ({recu // (1024 * 1024)} Mo)",
            recu / (1024 * 1024), total / (1024 * 1024), cle="phase.update_download")

    with _en_langue_courante():
        _archive().telecharger(
            url, destination, taille, sha256, depot=DEPOT, agent=_agent(),
            ouvrir=urllib.request.urlopen, contexte=contexte_tls(),
            progression=progression, taille_max=MAX_ARCHIVE_BYTES)
    print(msg("archive_recue", nom=destination.name,
              mo=destination.stat().st_size // (1024 * 1024)))


def _extraire(archive: Path, vers: Path) -> Path:
    """Unpacks the archive and returns the bundle folder it contained (published
    archives contain a single « blink2video » folder)."""
    with _en_langue_courante():
        return _archive().extraire(archive, vers, taille_max=MAX_EXTRACTED_BYTES,
                                   membres_max=MAX_ARCHIVE_MEMBERS)


def _executable(dossier: Path) -> Path:
    nom = "blink2video.exe" if sys.platform == "win32" else "blink2video"
    return dossier / nom


def _creer_dossier_travail(installe: Path, version: str) -> Path:
    """Creates ``installe/update/<version>`` without reusing a preparation.

    The marker distinguishes our directory from a possible homonymous folder
    created by the user: recursive cleanup never touches a folder it does
    not recognize as belonging to blink2video.
    """
    nom_version = str(version).strip().lstrip("vV")
    if (not nom_version or nom_version in (".", "..")
            or not all(c.isascii() and (c.isalnum() or c in ".-_")
                       for c in nom_version)):
        raise OSError(msg("version_dossier_impropre", version=version))

    racine = installe / DOSSIER_TRAVAIL
    marqueur = racine / MARQUEUR_TRAVAIL
    if racine.exists():
        if not racine.is_dir():
            raise OSError(msg("dossier_deja_existant", racine=racine))
        if not marqueur.is_file():
            # An interruption between mkdir() and writing the marker leaves
            # an empty folder: it is safe to resume this exact case.
            if any(racine.iterdir()):
                raise OSError(msg("dossier_deja_existant", racine=racine))
            marqueur.write_text(
                "Répertoire temporaire de mise à jour.\n", encoding="utf-8"
            )
    else:
        racine.mkdir()
        marqueur.write_text("Répertoire temporaire de mise à jour.\n", encoding="utf-8")
    travail = racine / nom_version
    # Without exist_ok: a second simultaneous update must never write
    # into the first's archive or partial files.
    travail.mkdir()
    return travail


def _ligne(dossier: Path, *arguments: str) -> list:
    """Command that launches blink2video installed in this folder.

    From a bundle it is an executable; from sources, the interpreter
    and the script. The rest of this module ignores the difference."""
    if runtime.frozen():
        return [str(_executable(dossier)), *arguments]
    return [sys.executable, "-u", str(dossier / f"{runtime.ENTREE}.py"), *arguments]


def _rendre_executable(dossier: Path) -> None:
    """A zip does not carry the executable bit: macOS loses it, and
    Gatekeeper quarantine is added to every downloaded archive."""
    if sys.platform == "win32":
        return
    cible = _executable(dossier)
    if cible.is_file():
        cible.chmod(0o755)
    if sys.platform == "darwin":
        runtime.lancer(["xattr", "-dr", "com.apple.quarantine", str(dossier)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)


def _verifier(dossier: Path, attendue: str) -> bool:
    """Does the new executable start, and does it report the right version?

    That is the guardrail of the whole operation: until it has answered, nothing
    is replaced. A truncated or system-incompatible archive
    fails here, on an installation still intact."""
    binaire = _executable(dossier)
    if not binaire.is_file():
        print(msg("echec_binaire_absent", nom=binaire.name))
        return False
    try:
        sortie = runtime.lancer([str(binaire), "--version"], capture_output=True,
                                text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as erreur:
        print(msg("echec_binaire_ne_demarre_pas", erreur=erreur))
        return False
    annonce = (sortie.stdout or "").strip()
    annonce_attendue = f"blink2video {attendue}"
    if sortie.returncode != 0 or annonce != annonce_attendue:
        print(msg("echec_binaire_annonce", annonce=annonce or "?", attendue=annonce_attendue))
        return False
    print(msg("binaire_verifie", annonce=annonce))
    return True


# What belongs to the program, and what the update replaces. Everything else
# in the folder (clips, videos, registries, Blink session) belongs to
# the user and is never touched.
CONTENU_DU_PROGRAMME = ("blink2video.exe", "blink2video", "_internal")
# Historical names, kept to recognize state left by an older
# version: the common package adds « .lock » to the reservation name.
MARQUEUR_PERMUTATION = ".blink_maj_permutation.json"
NOM_RESERVATION = ".blink_maj-installation"

# Without the common package (python -S), nothing permutes: the class exists only so
# this module's « except » clauses stay valid.
RestaurationIncomplete = (maj_install.RestaurationIncomplete if maj_install
                          else type("RestaurationIncomplete", (RuntimeError,), {}))


def _langue() -> str:
    return runtime.lire_langue()


@contextlib.contextmanager
def _reservation_installation(installe: Path):
    """Serializes cleanup and permutation, independently of storage."""
    with maj_install.reservation(installe, NOM_RESERVATION, langue=_langue()):
        yield


def _poser(source: Path, cible: Path) -> None:
    maj_install.poser(source, cible)


def _permuter(neuf: Path, installe: Path) -> bool:
    """Puts the new files in place of the old ones, or restores everything
    (maj_install.permuter)."""
    return maj_install.permuter(
        neuf, installe, CONTENU_DU_PROGRAMME, marqueur=MARQUEUR_PERMUTATION,
        nom_reservation=NOM_RESERVATION, poser=lambda source, cible: _poser(source, cible),
        ecrire=lambda message: print(message, flush=True), langue=_langue())


def _nettoyer_travail(installe: Path) -> None:
    """What is specific to blink2video in the cleanup: the preparation folder
    and preparations created next to the installation by earlier
    versions (they all carried this reserved prefix)."""
    travail = installe / DOSSIER_TRAVAIL
    if (travail / MARQUEUR_TRAVAIL).is_file():
        shutil.rmtree(travail, ignore_errors=True)
    for reste in installe.parent.glob(f"{PREFIXE_TRAVAIL_HISTORIQUE}*"):
        shutil.rmtree(reste, ignore_errors=True)


def _nettoyer(installe: Path) -> bool:
    """Erases leftovers from a previous update, at the start of the next one:
    the program that permutes runs from ``update``, and under Windows an
    executable cannot erase the folder it came from. Returns true if an
    old-style permutation was interrupted: it will be resumed by the old
    path, the only one that knows how to finish it."""
    return maj_install.nettoyer_restes(
        installe, CONTENU_DU_PROGRAMME, marqueur=MARQUEUR_PERMUTATION,
        nom_reservation=NOM_RESERVATION, apres=_nettoyer_travail, langue=_langue())


def _relancer(installe: Path, verbes: list) -> None:
    """Puts the machine back in the state the update found it in.

    We relaunch what was running, verb by verb, rather than the recommended
    composition: someone who had only launched the interface does not want to
    end up with four loops."""
    ligne = _ligne(installe, *[mot for groupe in verbes for mot in groupe])
    if not verbes:
        ligne.append("start")
    print(msg("relance", commande=" ".join(ligne)), flush=True)
    env = dict(os.environ)
    if env.pop("BLINK_UPDATE_AUTO_HOME", "") == "1":
        # Only the finalizer needed a forced data root.
        # The installed program must again find its state on its own.
        env.pop("BLINK_HOME", None)
    # Same for the fiches: only the finalizer had to look for them where
    # the replaced version stored them. Until 0.13, that was anyway
    # the default root; since 0.14, keeping one from an earlier
    # version would put the relaunched instance away from the state folder, where stop
    # will look for it next.
    env.pop("BLINK_CONTROL_HOME", None)
    maj_install.relancer_verbes(
        Path(ligne[0]), [ligne[1:]], cwd=installe, env=env,
        demarrer=lambda commande, **options: runtime.demarrer(
            commande, start_new_session=(os.name != "nt"), **options))


def finaliser(cible: Path) -> int:
    """Finds the fiches of the version to replace, wherever it stored them.

    A recent launcher provides BLINK_CONTROL_HOME. An older one only passes
    BLINK_HOME (data), even when the fiches are at the installation, and
    « update » from sources passes nothing. Candidates, therefore: the default
    root (the state folder since 0.14, whose migration also moves
    fiches of an older instance still running),
    the installation (≤ 0.13) and BLINK_HOME. Do not confuse an empty search
    with a successful stop. If several roots carry fiches,
    refuse to guess which set to stop.
    """
    # Under the systemd service, this process is part of what « stop »
    # will bring down: the rest continues outside the unit (issue #35).
    # Here rather than at finalizer launch, because it is the old
    # version that launches it: an update from a version that does not know
    # how to leave the service still benefits.
    import autostart
    if autostart.sortir_du_service(
            runtime.self_command("update", "--finaliser", str(cible))):
        print(msg("hors_du_service"), flush=True)
        return 0
    if os.environ.get("BLINK_CONTROL_HOME"):
        return _finaliser(cible)
    installe = cible.resolve()
    home = os.environ.get("BLINK_HOME")
    candidats = [runtime._dossier_controle(), installe]
    if home:
        candidats.append(Path(home).expanduser().resolve())
    candidats = list(dict.fromkeys(candidats))
    try:
        references = [racine for racine in candidats
                      if any((racine / runtime.INSTANCES).glob("*.json"))]
    except OSError:
        print(msg("racine_controle_illisible"), flush=True)
        return 1
    if len(references) > 1:
        print(msg("racines_controle_multiples"), flush=True)
        return 1
    controle = references[0] if references else candidats[0]
    ancien_controle = os.environ.get("BLINK_CONTROL_HOME")
    ancien_auto = os.environ.get("BLINK_UPDATE_AUTO_HOME")
    os.environ["BLINK_CONTROL_HOME"] = str(controle)
    if (home and controle == installe and Path(home).expanduser().resolve()
            == runtime.app_dir_depuis(installe)):
        os.environ["BLINK_UPDATE_AUTO_HOME"] = "1"
    try:
        return _finaliser(cible)
    finally:
        for nom, ancienne in (("BLINK_CONTROL_HOME", ancien_controle),
                               ("BLINK_UPDATE_AUTO_HOME", ancien_auto)):
            if ancienne is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = ancienne


def _compositions_en_cours() -> list:
    """What is running, noted before the stop: that is what must be relaunched."""
    fiches = runtime.lire_instances()
    # Children of a supervisor (start) also have their fiche, but it is
    # the supervisor that recreates them: relaunching them as well doubled watch and download
    # after every update. An orphan child, listed by no
    # fiche, is still relaunched.
    enfants = {pid for fiche in fiches for pid in (fiche.get("enfants") or [])}
    compositions = []
    vues = set()
    for fiche in fiches:
        if fiche.get("pid") in enfants:
            continue
        verbes = fiche.get("verbes") or []
        signature = tuple(tuple(groupe) for groupe in verbes)
        if signature and signature not in vues:
            vues.add(signature)
            compositions.append(verbes)
    if not compositions:
        compositions.append([])  # Same fallback to start when no instance.
    return compositions


def _finaliser(cible: Path) -> int:
    """Second stage, run by the new version from its temporary
    folder: stop, replace, relaunch (maj_install.finaliser)."""
    installe = cible.resolve()
    neuf = Path(sys.executable).resolve().parent if runtime.frozen() \
        else Path(__file__).resolve().parent

    def arreter() -> bool:
        # The old version of stop does not know BLINK_CONTROL_HOME. Also
        # passing this root via BLINK_HOME avoids it looking for
        # fiches in redirected data, then announcing « nothing is running ».
        env_arret = dict(os.environ, BLINK_HOME=str(runtime._dossier_controle()))
        arret = runtime.lancer(_ligne(installe, "stop"), cwd=str(installe),
                               env=env_arret, stdin=subprocess.DEVNULL, check=False)
        return arret.returncode == 0

    def dire(cle: str, echec: bool, **valeurs) -> None:
        texte = valeurs["texte"] if cle == "brut" else msg(cle, **valeurs)
        if echec:
            _conclure_sans_relance(texte)
        else:
            print(texte, flush=True)

    # From sources, « git pull » has already put the files in place: there is
    # nothing to permute, only to relaunch (neuf == installe).
    return maj_install.finaliser(
        installe, neuf, CONTENU_DU_PROGRAMME,
        noter=_compositions_en_cours, arreter=arreter,
        # lire_instances also keeps fiches where only a child or an
        # ffmpeg survives: the supervisor's death is not enough.
        vivants=lambda: bool(runtime.lire_instances()),
        relancer=lambda compositions: _relancer_tout(installe, compositions),
        dire=dire, marqueur=MARQUEUR_PERMUTATION, nom_reservation=NOM_RESERVATION,
        permutation=lambda source, destination: _permuter(source, destination),
        dormir=lambda secondes: time.sleep(secondes), langue=_langue())


def _relancer_tout(installe: Path, compositions: list) -> None:
    """Relaunches what was running. Having left a systemd unit to survive
    its stop (autostart.sortir_du_service), that is what we relaunch:
    blink2video returns under its service's guard, rather than running
    detached in the transient scope. Otherwise, each composition, as
    before."""
    import autostart
    unite = autostart.relancer_service()
    if unite:
        print(msg("relance", commande=f"systemctl --user start {unite}"), flush=True)
        return
    for verbes in compositions:
        _relancer(installe, verbes)


def _depuis_les_sources() -> int:
    """Update of a plain-source installation: the repository acts as the archive.

    Same flow as for a bundle, with « git pull » instead of
    download, and the same rule: touch nothing until the new
    version is there, and it is the one that stops and relaunches."""
    dossier = Path(__file__).resolve().parent
    if not (dossier / ".git").exists():
        _conclure_sans_relance(msg("pas_un_depot_git"))
        return 2

    neuve = disponible(force=True)
    if not neuve:
        _conclure_sans_relance(msg("deja_a_jour", version=_version_locale()))
        return 0

    print(msg("maj_git_pull", ancienne=_version_locale(), neuve=neuve['version']))
    tire = runtime.lancer(["git", "pull", "--ff-only"], cwd=str(dossier),
                          capture_output=True, text=True, check=False)
    print((tire.stdout or "").strip() or (tire.stderr or "").strip())
    if tire.returncode != 0:
        _conclure_sans_relance(msg("git_pull_refuse"))
        return 1

    # Our own VERSION is the one from before the pull: it is the file on
    # disk that says what just arrived.
    obtenue = _version_locale()
    if obtenue != neuve["version"]:
        _conclure_sans_relance(
            msg("depot_version_inattendue", obtenue=obtenue or "?", neuve=neuve['version']))
        return 1

    print(msg("passage_nouvelle_version"))
    runtime.demarrer(
        [sys.executable, "-u", str(dossier / "maj.py"), "--finaliser", str(dossier)],
        cwd=str(dossier), stdin=subprocess.DEVNULL,
        stdout=(runtime.app_dir() / "maj.log").open("ab"), stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"))
    return 0


def installer(force: bool = False) -> int:
    """First stage: look up, download, verify, then hand over."""
    if runtime.build_windows7():
        print(msg("message_windows7"))
        return 0
    if not runtime.frozen():
        return _depuis_les_sources()

    installe = Path(sys.executable).resolve().parent
    try:
        interrompue = _nettoyer(installe)
    except RestaurationIncomplete as erreur:
        _conclure_sans_relance(str(erreur))
        return 1

    # Since 0.23, the common installer (the same as lidar2map, gpxsolar and
    # watch2notif: whole-folder swap by an external assistant, verification of
    # the new version after relaunch, inter-process lock). Two cases keep
    # the old file-by-file permutation: an interrupted permutation, which it
    # alone knows how to resume, and an installation the common installer refuses to replace
    # (parent folder write-protected, unexpected contents): permutation only
    # needs the installation folder.
    if not interrompue:
        possible, raison = _possible_commun()
        if possible:
            return _lancer_installateur_commun()
        print(msg("maj_ancien_chemin", raison=raison))

    neuve = disponible(force=True)
    if not neuve:
        _conclure_sans_relance(msg("deja_a_jour", version=runtime.VERSION))
        return 0
    archive = neuve.get("archive") or {}
    if not archive.get("url"):
        _conclure_sans_relance(
            msg("archive_absente_pour_ce_systeme", version=neuve['version'],
                page=neuve.get('page')))
        return 1

    print(msg("maj_vers", ancienne=runtime.VERSION, neuve=neuve['version']))
    travail = None
    try:
        nom_archive = _nom_archive_sur(archive.get("nom"))
        empreinte = _empreinte_attendue(archive)
        taille = int(archive.get("taille") or 0)
        travail = _creer_dossier_travail(installe, neuve["version"])
        fichier = travail / nom_archive
        _telecharger(str(archive["url"]), fichier, taille, empreinte)
        runtime.travail("Installation de la mise à jour", 0, 0, cle="phase.update_install")
        dossier = _extraire(fichier, travail / "contenu")
        _rendre_executable(dossier)
        if not _verifier(dossier, neuve["version"]):
            _conclure_sans_relance(msg("echec_maj_binaire_incorrect"))
            return 1
        fichier.unlink(missing_ok=True)
    except (OSError, urllib.error.URLError, zipfile.BadZipFile,
            tarfile.TarError) as erreur:
        _conclure_sans_relance(
            msg("echec_maj_exception", type=type(erreur).__name__, erreur=erreur))
        if travail is not None:
            shutil.rmtree(travail, ignore_errors=True)
        return 1
    runtime.fin_travail()

    # The rest belongs to the new version: it alone can replace
    # this one without sawing off the branch. Detached, because this process is part
    # of what it will stop.
    print(msg("passage_nouvelle_version"))
    # The temporary finalizer must know data and control
    # fiches separately. Forcing data via BLINK_HOME alone also changed
    # process lookup and made the old instance invisible.
    # Naming them explicitly also protects against a default location
    # change in the arriving version (0.14 moved state). Honour a
    # BLINK_HOME provided by the user, otherwise remove this temporary
    # override on relaunch.
    env = dict(os.environ, BLINK_HOME=str(runtime.app_dir()),
               BLINK_CONTROL_HOME=str(runtime._dossier_controle()))
    if not os.environ.get("BLINK_HOME"):
        env["BLINK_UPDATE_AUTO_HOME"] = "1"
    runtime.demarrer(
        [str(_executable(dossier)), "update", "--finaliser", str(installe)],
        cwd=str(dossier), env=env,
        stdin=subprocess.DEVNULL,
        stdout=(runtime.app_dir() / "maj.log").open("ab"), stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"))
    return 0


# ------------------------------------------------- common installer (0.23)

def _application_commune():
    """What the common installer must know about blink2video. Its data lives outside
    the installation folder since 0.14: nothing to copy. Tolerated names are possible
    leftovers of the old mechanism. The systemd service or launchd agent are
    those under which THIS process runs (the name depends on the launched composition):
    the assistant then relaunches them by name."""
    import autostart
    label = os.environ.get("XPC_SERVICE_NAME", "")
    return maj_install.Application(
        "blink2video",
        noms_toleres=(f"{NOM_RESERVATION}.lock", NOM_RESERVATION, DOSSIER_TRAVAIL),
        unite_systemd=autostart.unite_systemd() if sys.platform.startswith("linux") else "",
        label_launchd=label if label.startswith("com.nico579.blink2video") else "",
        fenetre="Minimized")


def _disposition_commune(app=None):
    """Layout of the current installation. Archive names follow the common
    convention (blink2video-<system>-<arch>), but their root folder is called
    simply « blink2video » on all systems."""
    app = app or _application_commune()
    fichier, genre, _racine = maj_install.archive_standard(
        "blink2video", racine_macos="blink2video")
    return maj_install.disposition(app, asset_name=fichier, archive_kind=genre,
                                   racine_attendue="blink2video")


def _possible_commun() -> tuple:
    """(true, "") if the common installer can replace this installation."""
    if maj_install is None:
        return False, "source_mode"
    return maj_install.possible(_disposition_commune)


def _lancer_installateur_commun() -> int:
    """First stage: relaunch detached, then return immediately. Launched by
    « serve » (page button) or by the icon menu, this process is part of
    the tree that « stop » will kill (taskkill /T under Windows): the second stage, whose
    parent has already gone, escapes it and can drive the stop through."""
    runtime.demarrer(
        runtime.self_command("update", "--commun"), cwd=str(runtime.app_dir()),
        stdin=subprocess.DEVNULL,
        stdout=(runtime.app_dir() / "maj.log").open("ab"), stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"))
    return 0


def _pids_en_cours() -> list:
    """All blink2video processes noted in the fiches (supervisors and their
    children): the assistant waits for each to end before swapping folders."""
    pids = []
    for fiche in runtime.lire_instances():
        for pid in [fiche.get("pid"), *(fiche.get("enfants") or [])]:
            if isinstance(pid, int) and pid > 0 and pid != os.getpid():
                pids.append(pid)
    return list(dict.fromkeys(pids))


def _verbes_a_relancer() -> list:
    """What was running, one command line per composition, like _relancer():
    the verbs of a composition in sequence, « start » if nothing was running."""
    return [[mot for groupe in verbes for mot in groupe] or ["start"]
            for verbes in _compositions_en_cours()]


def _arreter_tout(installe: Path) -> None:
    """Stops all instances, like the finalizer of the old path."""
    runtime.lancer(_ligne(installe, "stop"), cwd=str(installe), stdin=subprocess.DEVNULL,
                   check=False)


def _installer_commun() -> int:
    """Second stage: the common installer drives everything (lock, rights, cleanup,
    verified download, new-version self-test, external assistant), then
    blink2video stops; the assistant waits for each process to end, swaps the
    folders, relaunches what was running and checks the new version holds, otherwise
    restores the old one."""
    installe = Path(sys.executable).resolve().parent
    app = _application_commune()
    verificateur = _verificateur()
    print(msg("maj_commun"), flush=True)
    installateur = maj_install.Installateur(
        app, DEPOT, verificateur, lambda: _disposition_commune(app),
        quitter=lambda: _arreter_tout(installe), contexte=contexte_tls(),
        ecrire=lambda message: print(message, flush=True),
        verbes_relance=_verbes_a_relancer, pids_a_attendre=_pids_en_cours)
    if not installateur.demarrer():
        return 1
    # The page follows the same channel as before: the download bar, then
    # installation, then the server disappearing and coming back.
    while installateur._fil is not None and installateur._fil.is_alive():
        etat = installateur.etat()
        if etat["etat"] == "telechargement" and etat["total"]:
            runtime.travail(
                f"Téléchargement de la mise à jour ({etat['recu'] // (1024 * 1024)} Mo)",
                etat["recu"] / (1024 * 1024), etat["total"] / (1024 * 1024),
                cle="phase.update_download")
        time.sleep(0.5)
    etat = installateur.etat()
    if etat["etat"] == "redemarrage":
        return 0
    if etat["etat"] == "erreur":
        erreur = etat["erreur"] or {}
        texte = (erreur.get("message") or {}).get(_langue()) or str(erreur)
        _conclure_sans_relance(msg("echec_maj_commun", erreur=texte))
        return 1
    _conclure_sans_relance(msg("deja_a_jour", version=runtime.VERSION))
    return 0


def main() -> int:
    analyseur = argparse.ArgumentParser(
        prog="blink2video update",
        description=msg("aide_desc"))
    analyseur.add_argument("--check", action="store_true", help=msg("aide_check"))
    analyseur.add_argument("--finaliser", metavar="DOSSIER",
                           help=argparse.SUPPRESS)  # internal use
    analyseur.add_argument("--commun", action="store_true",
                           help=argparse.SUPPRESS)  # internal use
    arguments = analyseur.parse_args()

    if arguments.finaliser:
        # Still called by a version ≤ 0.22 that installs this one: it downloads,
        # then hands over to the new one with « update --finaliser » (transition).
        return finaliser(Path(arguments.finaliser))
    if arguments.commun:
        return _installer_commun()
    if runtime.build_windows7():
        print(msg("message_windows7"))
        return 0
    if arguments.check:
        neuve = disponible(force=True)
        if neuve:
            print(msg("version_disponible", version=neuve['version'],
                      actuelle=runtime.VERSION, page=neuve.get('page')))
        else:
            print(msg("deja_a_jour", version=runtime.VERSION))
        return 0
    return installer()


if __name__ == "__main__":
    sys.exit(main())
