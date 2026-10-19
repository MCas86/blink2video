"""Issue #49 : dire POURQUOI la validation d'un clip a échoué, sans chemin ni nom
de clip, et sans changer la décision. Les lignes de ffprobe sont celles
capturées sur de vrais clips (paquet Linux 0.15.4, VM Ubuntu 26.04)."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_engine
import merge_daily

from test_merge_daily_valid_mp4 import MP4_STRUCTUREL


def reponse(stdout="", stderr="", code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class RaisonDuRefus(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory(prefix="blink_raison_")
        self.addCleanup(self.dossier.cleanup)
        self.chemin = Path(self.dossier.name) / "clip-prive-1234.mp4"
        self.chemin.write_bytes(MP4_STRUCTUREL)

    def avec_sonde(self, genre, **reponse_ffprobe):
        return mock.patch.object(merge_daily, "_outil_validation_media",
                                 return_value=(genre, genre)), \
            mock.patch.object(merge_daily.runtime, "lancer", return_value=reponse(**reponse_ffprobe))

    def raison(self, genre="ffprobe", **kw):
        outil, lancer = self.avec_sonde(genre, **kw)
        with outil, lancer:
            return merge_daily.raison_refus_mp4(self.chemin), merge_daily.valid_mp4_complet(self.chemin)

    def test_un_fichier_valide_ne_donne_aucun_refus(self):
        raison, valide = self.raison(stdout="922\n")
        self.assertTrue(valide)
        self.assertIn("valid", raison)

    def test_le_bruit_connu_du_clip_usb_reste_accepte(self):
        raison, valide = self.raison(
            stdout="922\n", stderr="[h264 @ 0x5df4102b1480] missing picture in access unit with size 9\n")
        self.assertTrue(valide)

    def test_fichier_qui_n_est_pas_un_mp4(self):
        self.chemin.write_bytes(b"<html>erreur</html>")
        raison, valide = self.raison(stdout="1\n")
        self.assertFalse(valide)
        self.assertTrue(raison.startswith("structure="), raison)

    def test_aucune_sonde_disponible(self):
        with mock.patch.object(merge_daily, "_outil_validation_media", return_value=None):
            self.assertFalse(merge_daily.valid_mp4_complet(self.chemin))
            self.assertTrue(merge_daily.raison_refus_mp4(self.chemin).startswith("tool=neither"))

    def test_sonde_impossible_a_lancer(self):
        with mock.patch.object(merge_daily, "_outil_validation_media",
                               return_value=("ffprobe", "ffprobe")), \
             mock.patch.object(merge_daily.runtime, "lancer", side_effect=PermissionError("refuse")):
            self.assertFalse(merge_daily.valid_mp4_complet(self.chemin))
            self.assertEqual(merge_daily.raison_refus_mp4(self.chemin),
                             "outil=ffprobe lancement=PermissionError")

    def test_vraie_ligne_de_coupure_est_rapportee(self):
        raison, valide = self.raison(
            stdout="397\n", stderr="[h264 @ 0x59c11469e880] Invalid NAL unit size (3302 > 762).\n")
        self.assertFalse(valide)
        self.assertEqual(raison, "outil=ffprobe ligne=[h264 @ ADR] Invalid NAL unit size (3302 > 762).")

    def test_code_de_retour_non_nul(self):
        raison, valide = self.raison(code=1, stderr=f"{self.chemin}: Invalid data found when processing input\n")
        self.assertFalse(valide)
        self.assertTrue(raison.startswith("outil=ffprobe code=1 ligne="), raison)

    def test_aucun_paquet_video(self):
        raison, valide = self.raison(stdout="0\n")
        self.assertFalse(valide)
        self.assertEqual(raison, "outil=ffprobe video-packets=0")

    def test_sortie_illisible(self):
        raison, valide = self.raison(stdout="N/A\n")
        self.assertFalse(valide)
        self.assertEqual(raison, "outil=ffprobe output=unreadable")

    def test_ffmpeg_seul_rapporte_son_code(self):
        raison, valide = self.raison(genre="ffmpeg", code=1, stderr="Invalid NAL unit size (1 > 0)\n")
        self.assertFalse(valide)
        self.assertTrue(raison.startswith("outil=ffmpeg code=1"), raison)

    def test_aucun_chemin_ni_nom_de_clip_dans_la_raison(self):
        sources = [
            f"{self.chemin}: Invalid data found",
            f"Error opening {self.chemin.name}",
            f"cannot read {self.chemin.parent}/autre.mp4 now",
            "C:\\Users\\Nico\\Documents\\blink\\clip.mp4: erreur",
            "/home/vboxuser/clips/usb/Bureau/2026-09/x.mp4: erreur",
        ]
        for source in sources:
            with self.subTest(source=source):
                raison, _ = self.raison(code=1, stderr=source + "\n")
                for secret in (self.chemin.name, str(self.chemin.parent), "vboxuser", "Nico",
                               "Documents", "clip-prive"):
                    self.assertNotIn(secret, raison)

    def test_la_raison_est_bornee(self):
        raison, _ = self.raison(code=1, stderr="x" * 5000 + "\n")
        self.assertLess(len(raison), 200)

    def test_le_diagnostic_ne_leve_jamais(self):
        with mock.patch.object(merge_daily, "_motif_refus_mp4_complet", side_effect=RuntimeError("boum")):
            self.assertEqual(merge_daily.raison_refus_mp4(self.chemin),
                             "diagnosis impossible (RuntimeError)")


class RaisonImprimeeParLeTelechargement(unittest.IsolatedAsyncioTestCase):
    async def test_l_etape_validation_dit_aussi_pourquoi(self):
        with tempfile.TemporaryDirectory() as dossier:
            cible = Path(dossier) / "nom-prive.mp4"

            async def transfert(_blink, chemin):
                Path(chemin).write_bytes(MP4_STRUCTUREL)
                return True

            clip = SimpleNamespace(prepare_download=mock.AsyncMock(return_value=True),
                                   download_video=mock.AsyncMock(side_effect=transfert))
            texte = io.StringIO()
            with mock.patch.object(blink_engine.md, "valid_mp4_complet", return_value=False), \
                 mock.patch.object(blink_engine.md, "raison_refus_mp4",
                                   return_value="outil=ffprobe ligne=[h264 @ ADR] Invalid NAL unit size (1 > 0)"), \
                 contextlib.redirect_stdout(texte):
                resultat = await blink_engine.download_clip(object(), clip, cible, False)
            sortie = texte.getvalue()
            self.assertEqual(resultat, "failed")
            self.assertIn("=validation", sortie)
            self.assertIn("Invalid NAL unit size", sortie)
            self.assertNotIn("nom-prive", sortie)
            self.assertNotIn(dossier, sortie)

    async def test_une_raison_qui_plante_n_empeche_pas_l_echec_d_etre_rendu(self):
        with tempfile.TemporaryDirectory() as dossier:
            async def transfert(_blink, chemin):
                Path(chemin).write_bytes(b"x")
                return True

            clip = SimpleNamespace(prepare_download=mock.AsyncMock(return_value=True),
                                   download_video=mock.AsyncMock(side_effect=transfert))
            with mock.patch.object(blink_engine.md, "valid_mp4_complet", return_value=False), \
                 mock.patch.object(blink_engine.md, "raison_refus_mp4", side_effect=RuntimeError("boum")), \
                 contextlib.redirect_stdout(io.StringIO()):
                resultat = await blink_engine.download_clip(object(), clip, Path(dossier) / "a.mp4", False)
            self.assertEqual(resultat, "failed")

    async def test_les_deux_langues_ont_le_message(self):
        for langue in ("fr", "en"):
            with mock.patch.object(blink_engine.runtime, "lire_langue", return_value=langue):
                self.assertIn("X", blink_engine.msg("usb_echec_raison", raison="X"))


if __name__ == "__main__":
    unittest.main()
