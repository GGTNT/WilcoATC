"""An incomplete voice download must remain repairable from the panel."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wilcoatc.audio.voices import (
    CATALOGUE, KOKORO_DIR, KOKORO_MODEL, KOKORO_VOICES,
    SUPERTONIC_DIR, SUPERTONIC_FILES, SUPERTONIC_SENTINEL,
    VoiceRegistry, core_voice_packages, download_supertonic,
    supertonic_files_present,
)
from wilcoatc.config import Config
from wilcoatc.install import Installer, _voices_present


class VoicePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config()
        self.config.voice.voice_dir = str(self.root)

    def write(self, relative, content=b"model"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def piper(self, spec):
        self.write(spec.filename)
        self.write(spec.filename + ".json", b"{}")

    def core_piper(self):
        for spec in CATALOGUE:
            if spec.core:
                self.piper(spec)

    def supertonic(self):
        for name in SUPERTONIC_FILES:
            self.write(Path(SUPERTONIC_DIR) / name)

    def test_french_only_install_is_missing_english_and_supertonic(self):
        self.piper(next(v for v in CATALOGUE if v.key == "fr_FR-upmc-medium"))
        self.assertTrue(VoiceRegistry(self.root).available())
        self.assertFalse(_voices_present(self.config))
        installer = Installer(self.config)
        self.assertIn("voices", installer.missing())
        state = installer.state()
        missing = {p["key"] for p in state["voice_packages"] if not p["installed"]}
        self.assertIn("supertonic", missing)
        self.assertIn("piper-gb", missing)
        self.assertFalse(state["installed"]["voices"])

    def test_retired_kokoro_files_do_not_count_as_complete(self):
        self.write(Path(KOKORO_DIR) / KOKORO_MODEL)
        self.write(Path(KOKORO_DIR) / KOKORO_VOICES)
        self.config.voice.engine = "kokoro"
        self.assertFalse(_voices_present(self.config))

    def test_complete_core_does_not_require_optional_accents_or_clones(self):
        self.core_piper()
        self.supertonic()
        self.assertTrue(_voices_present(self.config))
        self.assertTrue(all(p["installed"] for p in core_voice_packages(self.root)))
        self.assertNotIn("voices", Installer(self.config).missing())

    def test_piper_only_setup_does_not_require_supertonic(self):
        self.core_piper()
        self.config.voice.engine = "piper"
        self.assertTrue(_voices_present(self.config))
        self.assertNotIn("supertonic", {
            p["key"] for p in Installer(self.config).state()["voice_packages"]})

    def test_piper_model_without_metadata_is_not_installed_or_cast(self):
        spec = next(v for v in CATALOGUE if v.key == "en_GB-alan-medium")
        self.write(spec.filename)
        self.assertFalse(_voices_present(self.config))
        self.assertEqual(VoiceRegistry(self.root, engine="piper").available(), [])

    def test_empty_core_file_is_reported_missing(self):
        self.core_piper()
        self.supertonic()
        self.write("en_GB-alan-medium.onnx.json", b"")
        self.assertFalse(_voices_present(self.config))

    def test_supertonic_requires_every_graph_and_speaker(self):
        self.supertonic()
        for name in ["onnx/text_encoder.onnx", "voice_styles/F1.json"]:
            with self.subTest(name=name):
                path = self.root / SUPERTONIC_DIR / name
                content = path.read_bytes()
                path.unlink()
                self.assertFalse(supertonic_files_present(self.root))
                self.assertFalse(VoiceRegistry(self.root).supertonic_installed())
                path.write_bytes(content)

    def test_partial_supertonic_is_repaired_despite_existing_sentinel(self):
        self.write(Path(SUPERTONIC_DIR) / SUPERTONIC_SENTINEL)

        def fetch(_repo, name, *, local_dir, **_kwargs):
            path = Path(local_dir) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"downloaded")
            return str(path)

        with patch("huggingface_hub.list_repo_files", return_value=list(SUPERTONIC_FILES)), \
                patch("huggingface_hub.hf_hub_download", side_effect=fetch) as download:
            download_supertonic(self.root, on_progress=lambda *_: None)
        self.assertEqual(download.call_count, len(SUPERTONIC_FILES))
        self.assertTrue(supertonic_files_present(self.root))

    def test_complete_supertonic_is_not_downloaded_again(self):
        self.supertonic()
        with patch("huggingface_hub.list_repo_files") as listing:
            download_supertonic(self.root)
        listing.assert_not_called()

    def test_repair_worker_keeps_existing_files_and_only_fetches_voices(self):
        self.config.voice.engine = "piper"
        spec = next(v for v in CATALOGUE if v.key == "en_GB-alan-medium")
        saved = self.write(spec.filename, b"existing model")
        updates = []

        def download(_url, target, **_kwargs):
            target.write_bytes(b"downloaded")

        with patch("wilcoatc.audio.voices._download", side_effect=download), \
                patch("wilcoatc.audio.voices.download_supertonic") as supertonic:
            installer = Installer(self.config, on_change=updates.append)
            self.assertTrue(installer.start(["voices"]))
            installer.wait(5)
        state = installer.state()
        self.assertFalse(state["running"])
        self.assertEqual(state["steps"][0]["state"], "done")
        self.assertTrue(state["installed"]["voices"])
        self.assertEqual(saved.read_bytes(), b"existing model")
        supertonic.assert_not_called()
        self.assertTrue(any(s["running"] for s in updates))
        self.assertFalse(updates[-1]["running"])

    def test_missing_package_messages_keep_translated_placeholder(self):
        folder = Path(__file__).resolve().parents[1] / "wilcoatc/web/static/i18n"
        catalogues = list(folder.glob("*.json"))
        self.assertEqual(len(catalogues), 41)
        for path in catalogues:
            with self.subTest(language=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["setup.missing_voices"].count("{packages}"), 1)
                self.assertTrue(data["setup.download_voices"].strip())

    def test_incomplete_download_is_a_retryable_failure(self):
        with self.assertLogs("wilcoatc.install", level="ERROR"), \
                patch("wilcoatc.audio.voices.download_voices", return_value=[]):
            installer = Installer(self.config)
            installer.start(["voices"])
            installer.wait(5)
        state = installer.state()
        self.assertEqual(state["steps"][0]["state"], "failed")
        self.assertIn("incomplete", state["steps"][0]["error"])
        self.assertIn("voices", state["missing"])


if __name__ == "__main__":
    unittest.main()
