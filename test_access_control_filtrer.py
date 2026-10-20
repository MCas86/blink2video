"""Allowlists + filtrer_* helpers for Cloudflare Access camera grants."""

from __future__ import annotations

import unittest

import access_control


CFG = {
    "admin_emails": ["admin@example.com"],
    "cloudflare": {
        "team_domain": "example.cloudflareaccess.com",
        "aud": "test-aud",
    },
    "users": [
        {
            "email": "friend@example.com",
            "cameras": ["Yard", "camera-key-yard"],
            "permissions": list(access_control.PERMISSIONS_INVITE),
        }
    ],
}


class TestAccessControlFiltrer(unittest.TestCase):
    def test_admin_voit_toutes_les_cameras(self):
        self.assertIsNone(
            access_control.cameras_autorisees("admin@example.com", CFG))
        self.assertTrue(access_control.camera_autorisee(
            "admin@example.com", "Anything", config=CFG))

    def test_invite_limite_a_allowlist(self):
        cams = access_control.cameras_autorisees("friend@example.com", CFG)
        self.assertEqual(cams, {"Yard", "camera-key-yard"})
        self.assertTrue(access_control.camera_autorisee(
            "friend@example.com", "Yard", config=CFG))
        self.assertTrue(access_control.camera_autorisee(
            "friend@example.com", "camera-key-yard", config=CFG))
        self.assertFalse(access_control.camera_autorisee(
            "friend@example.com", "Kitchen", config=CFG))

    def test_inconnu_sans_cameras(self):
        self.assertEqual(
            access_control.cameras_autorisees("stranger@example.com", CFG),
            set())
        profil = access_control.profil("stranger@example.com", CFG)
        self.assertEqual(profil["role"], "guest")
        self.assertTrue(profil["pending_allowlist"])

    def test_filtrer_system_state(self):
        etat = {
            "systems": [
                {
                    "name": "Home",
                    "cameras": [
                        {"name": "Yard", "key": "camera-key-yard"},
                        {"name": "Kitchen", "key": "camera-key-kitchen"},
                    ],
                },
                {
                    "name": "Empty after filter",
                    "cameras": [
                        {"name": "Kitchen", "key": "camera-key-kitchen"},
                    ],
                },
            ]
        }
        filtre = access_control.filtrer_system_state(
            etat, "friend@example.com", CFG)
        self.assertEqual(len(filtre["systems"]), 1)
        self.assertEqual(
            [c["name"] for c in filtre["systems"][0]["cameras"]], ["Yard"])
        self.assertEqual(
            access_control.filtrer_system_state(
                etat, "admin@example.com", CFG),
            etat)

    def test_filtrer_clips_videos_snapshots(self):
        clips = {
            "clips": [
                {"camera": "Yard", "cameraKey": "camera-key-yard"},
                {"camera": "Kitchen", "cameraKey": "camera-key-kitchen"},
            ]
        }
        self.assertEqual(len(access_control.filtrer_clips(
            clips, "friend@example.com", CFG)["clips"]), 1)
        videos = {
            "daily": [{"camera": "Yard"}, {"camera": "Kitchen"}],
            "weekly": [{"camera": "Kitchen"}],
        }
        filtre_v = access_control.filtrer_videos(
            videos, "friend@example.com", CFG)
        self.assertEqual(len(filtre_v["daily"]), 1)
        self.assertEqual(filtre_v["weekly"], [])
        snaps = [{"camera": "Yard"}, {"camera": "Kitchen"}]
        self.assertEqual(len(access_control.filtrer_snapshots(
            snaps, "friend@example.com", CFG)), 1)

    def test_cloudflare_inactif_sans_team_et_aud(self):
        cfg = {
            "admin_emails": ["admin@example.com"],
            "cloudflare": {"team_domain": "", "aud": ""},
            "users": [],
        }
        self.assertFalse(access_control.cloudflare_actif(cfg))


if __name__ == "__main__":
    unittest.main()
