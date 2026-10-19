"""Issue GitHub #6 (2026-09-13): command messages (download, login, merge...)
stayed in French unlike the web page. `runtime.traduire()` gives them the
same bilingual mechanism as LIBELLES in tray.py, indexed by
`runtime.lire_langue()`. This fork is English-only for catalogs: "fr" mirrors
"en", so both language codes yield English strings.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import runtime
import blink_auth
import blink_cli
import blink_models
import blink_engine
import merge_daily
import maj
import autostart
import smoketest
import watch
import serve


class TestTraduireLibelles(unittest.TestCase):
    def setUp(self):
        self._dossier = tempfile.TemporaryDirectory(prefix="blink_i18n_test_")
        self._ancien_home = os.environ.get("BLINK_HOME")
        os.environ["BLINK_HOME"] = self._dossier.name

    def tearDown(self):
        if self._ancien_home is None:
            os.environ.pop("BLINK_HOME", None)
        else:
            os.environ["BLINK_HOME"] = self._ancien_home
        self._dossier.cleanup()

    def _regler_langue(self, code: str) -> None:
        (Path(self._dossier.name) / runtime.LANGUE).write_text(code, encoding="utf-8")

    def test_defaut_anglais_sans_fichier_langue(self):
        self.assertEqual(runtime.lire_langue(), "en")
        self.assertEqual(blink_auth._("session_invalide"),
                         "The saved session is no longer valid.")

    def test_bascule_en_anglais(self):
        self._regler_langue("en")
        self.assertEqual(blink_auth._("session_invalide"),
                         "The saved session is no longer valid.")

    def test_formatage_avec_valeurs(self):
        self._regler_langue("en")
        self.assertEqual(blink_auth._("code_refuse", remaining=2),
                         "Code rejected. 2 attempt(s) left.")
        self._regler_langue("fr")
        # English-only fork: fr catalog mirrors en
        self.assertEqual(blink_auth._("code_refuse", remaining=2),
                         "Code rejected. 2 attempt(s) left.")

    def test_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(blink_auth.LIBELLES["fr"]), set(blink_auth.LIBELLES["en"]))

    def test_blink_cli_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(blink_cli.LIBELLES["fr"]), set(blink_cli.LIBELLES["en"]))

    def test_blink_cli_msg_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(
            blink_cli.msg("sync_module_ligne", nom="Garage", sync_id=1, network_id=2),
            "- Garage (ID 1, network 2)")
        self._regler_langue("fr")
        self.assertEqual(
            blink_cli.msg("sync_module_ligne", nom="Garage", sync_id=1, network_id=2),
            "- Garage (ID 1, network 2)")

    def test_blink_cli_pluriel_arrete_verbe(self):
        self._regler_langue("en")
        self.assertEqual(blink_cli.msg("arrete_verbe_code", verbe="download", code=1),
                         "Stopped: download (code 1)")
        self.assertEqual(blink_cli.msg("arrete_verbe_normal", verbe="download"),
                         "Stopped: download (normal exit)")
        self._regler_langue("fr")
        self.assertEqual(blink_cli.msg("arrete_verbe_normal", verbe="download"),
                         "Stopped: download (normal exit)")

    def test_blink_models_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(blink_models.LIBELLES["fr"]), set(blink_models.LIBELLES["en"]))

    def test_blink_models_human_size_suit_la_langue(self):
        self._regler_langue("fr")
        self.assertEqual(blink_models.human_size(1024), "1.0 KiB")
        self._regler_langue("en")
        self.assertEqual(blink_models.human_size(1024), "1.0 KiB")

    def test_blink_models_categorie_bascule(self):
        self._regler_langue("en")
        self.assertEqual(blink_models.msg("cat_reseau"), "network")
        self._regler_langue("fr")
        self.assertEqual(blink_models.msg("cat_reseau"), "network")

    def test_blink_engine_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(blink_engine.LIBELLES["fr"]), set(blink_engine.LIBELLES["en"]))

    def test_blink_engine_msg_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(blink_engine.msg("nouveaux_clips", n=3), "\nNew clips: 3")
        self._regler_langue("fr")
        self.assertEqual(blink_engine.msg("nouveaux_clips", n=3), "\nNew clips: 3")

    def test_blink_engine_notification_singulier_pluriel(self):
        self._regler_langue("en")
        self.assertEqual(blink_engine.msg("notif_corps_singulier", n=1),
                         "1 new clip downloaded. Click to open.")
        self.assertEqual(blink_engine.msg("notif_corps_pluriel", n=3),
                         "3 new clips downloaded. Click to open.")

    def test_merge_daily_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(merge_daily.LIBELLES["fr"]), set(merge_daily.LIBELLES["en"]))

    def test_merge_daily_msg_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(merge_daily.msg("mp4_invalide"), "FFmpeg did not produce a valid MP4")
        self.assertEqual(
            merge_daily.msg("jour_deja_a_jour", nom="Garage_2026-09-01.mp4", clips=5),
            "Already up to date: Garage_2026-09-01.mp4 (5 clip(s))")
        self._regler_langue("fr")
        self.assertEqual(merge_daily.msg("mp4_invalide"), "FFmpeg did not produce a valid MP4")

    def test_merge_daily_labels_periode(self):
        self._regler_langue("en")
        self.assertEqual(merge_daily.msg("label_hebdomadaires"), "Weekly")
        self.assertEqual(merge_daily.msg("label_mensuelles"), "Monthly")
        self._regler_langue("fr")
        self.assertEqual(merge_daily.msg("label_hebdomadaires"), "Weekly")
        self.assertEqual(merge_daily.msg("label_mensuelles"), "Monthly")

    def test_maj_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(maj.LIBELLES["fr"]), set(maj.LIBELLES["en"]))

    def test_maj_msg_bascule_et_formate(self):
        self._regler_langue("en")
        with tempfile.TemporaryDirectory() as dossier:
            archive = Path(dossier) / "mauvaise.zip"
            with zipfile.ZipFile(archive, "w") as sortie:
                sortie.writestr("../../etc/passwd", b"x")
            with self.assertRaises(OSError) as erreur:
                maj._extraire(archive, Path(dossier) / "contenu")
            self.assertEqual(str(erreur.exception),
                             "Dangerous path in the archive: '../../etc/passwd'")
        self.assertEqual(maj.msg("deja_a_jour", version="0.12.20"),
                         "blink2video 0.12.20 is up to date.")
        self._regler_langue("fr")
        self.assertEqual(maj.msg("deja_a_jour", version="0.12.20"),
                         "blink2video 0.12.20 is up to date.")

    def test_autostart_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(autostart.LIBELLES["fr"]), set(autostart.LIBELLES["en"]))

    def test_autostart_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(autostart._("demarrage_retire", cible="foo.lnk"),
                         "Autostart removed: foo.lnk")
        self.assertEqual(autostart._("intitule_compte", intitule="Startup shortcuts", n=2),
                         "Startup shortcuts: 2")
        self._regler_langue("fr")
        self.assertEqual(autostart._("demarrage_deja_absent", cible="foo.lnk"),
                         "Autostart already absent: foo.lnk")

    def test_smoketest_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(smoketest.LIBELLES["fr"]), set(smoketest.LIBELLES["en"]))

    def test_smoketest_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(smoketest._("clips_compte_detail", n=5, m=1), "5 clip(s), 1 excluded")
        self.assertEqual(smoketest._("marque_echec"), "FAIL ")
        self._regler_langue("fr")
        self.assertEqual(smoketest._("clips_compte_detail", n=5, m=1), "5 clip(s), 1 excluded")
        self.assertEqual(smoketest._("marque_echec"), "FAIL ")

    def test_watch_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(watch.MESSAGES["fr"]), set(watch.MESSAGES["en"]))

    def test_watch_msg_deja_existant_continue_de_fonctionner(self):
        self._regler_langue("en")
        self.assertEqual(watch._msg("camera_hors_ligne", nom="Garage"),
                         'Camera "Garage" offline.')
        self._regler_langue("fr")
        self.assertEqual(watch._msg("camera_hors_ligne", nom="Garage"),
                         'Camera "Garage" offline.')

    def test_watch_nouvelles_cles_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(watch._msg("alerte_ligne", ligne="Garage hors ligne."),
                         "ALERT    Garage hors ligne.")
        self.assertEqual(watch._msg("aucune"), "none")
        self._regler_langue("fr")
        self.assertEqual(watch._msg("cameras_en_sourdine"),
                         watch.MESSAGES["en"]["cameras_en_sourdine"])

    def test_serve_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(serve.LIBELLES["fr"]), set(serve.LIBELLES["en"]))

    def test_serve_msg_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(serve.msg("erreur_generique", erreur="ffmpeg introuvable"),
                         "Error: ffmpeg introuvable")
        self.assertEqual(
            serve.msg("echec_ecoute_port", port=8765, erreur="port deja utilise"),
            "Could not listen on port 8765: port deja utilise")
        self._regler_langue("fr")
        self.assertEqual(serve.msg("erreur_generique", erreur="ffmpeg introuvable"),
                         "Error: ffmpeg introuvable")

    def test_maj_restauration_incomplete_garde_le_message_traduit(self):
        self._regler_langue("en")
        try:
            raise maj.RestaurationIncomplete(
                maj.msg("maj_precedente_non_finalisee", marqueur="MARQUEUR"))
        except maj.RestaurationIncomplete as erreur:
            self.assertTrue(str(erreur).startswith(
                "Previous update not finalized: backups and preparation kept."))
            self.assertIn("delete the file MARQUEUR", str(erreur))

    def test_issue_95_une_mise_a_jour_interrompue_est_reprise_sans_rien_supprimer(self):
        self._regler_langue("en")
        with tempfile.TemporaryDirectory() as dossier:
            racine = Path(dossier)
            installe, neuf = racine / "installe", racine / "neuf"
            installe.mkdir()
            (neuf / "_internal").mkdir(parents=True)
            (neuf / "blink2video.exe").write_bytes(b"neuf")
            (neuf / "_internal" / "lib").write_bytes(b"lib-neuve")
            (installe / maj.MARQUEUR_PERMUTATION).write_text("{}", encoding="utf-8")
            (installe / "blink2video.exe.ancien").write_bytes(b"ancienne version")
            (installe / "blink2video.exe").write_bytes(b"a moitie")
            maj._nettoyer(installe)
            self.assertEqual((installe / "blink2video.exe.ancien").read_bytes(), b"ancienne version")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(maj._permuter(neuf, installe))
            self.assertEqual((installe / "blink2video.exe").read_bytes(), b"neuf")
            self.assertFalse((installe / maj.MARQUEUR_PERMUTATION).exists())
            maj._nettoyer(installe)
            self.assertFalse((installe / "blink2video.exe.ancien").exists())
            self.assertFalse((installe / "blink2video.exe.reprise").exists())

    def test_runtime_toutes_les_cles_existent_dans_les_deux_langues(self):
        self.assertEqual(set(runtime._LIBELLES_RUNTIME["fr"]),
                         set(runtime._LIBELLES_RUNTIME["en"]))

    def test_runtime_bascule_et_formate(self):
        self._regler_langue("en")
        self.assertEqual(runtime._msg("repetition", minutes=5),
                         "Repeating every 5 min. Ctrl+C to stop.")

    def test_aide_principale_liste_les_verbes_dans_la_langue_de_la_page(self):
        self._regler_langue("en")
        sortie = io.StringIO()
        with mock.patch.object(sys, "argv", ["blink2video", "--help"]), \
                contextlib.redirect_stdout(sortie), self.assertRaises(SystemExit):
            blink_cli.parse_args()
        aide = sortie.getvalue()
        self.assertIn(runtime.VERBES["login"].en, aide)
        self.assertNotIn(runtime.VERBES["login"].fr, aide)
        self.assertIn("Getting started:", aide)


if __name__ == "__main__":
    unittest.main()
