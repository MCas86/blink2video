# Test fixtures

`clip_usb_reel_neutre.mp4`: a real detection clip recorded on a Blink Sync Module USB stick (desk camera, 2026-10-05), 1280x720, 21.5 s, 1.6 MB, published as-is, byte for byte. It shows a dark object in front of a white shelf: no person, nothing identifiable. Its audio track is only microphone background noise (average level about -51 dB, peak -34 dB), verified before publication.

It serves one precise purpose: every clip recorded on a Sync Module USB stick contains an H.264 access unit with no picture, which `ffprobe` reports as `missing picture in access unit with size 9` even though the video is complete (issue #49). Clips made by FFmpeg or from the cloud do not have this quirk, which is why CI never saw this defect. See `test_validation_clip_usb_reel.py`.

Do not replace this file with an edited clip: the test checks its SHA-256 fingerprint.
