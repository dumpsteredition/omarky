from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import chat_card

PNG = chat_card.PNG_SIGNATURE + b"marked-png-bytes"


class ChatCardStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="omarky-card-test-")
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.runtime.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(self.runtime),
            "HOME": str(self.home),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temporary.cleanup)

    def test_save_and_load_exact_marked_png_and_literal_note_with_private_modes(self) -> None:
        note = "Keep the text literal.\nNo extra wording.  "
        card = chat_card.save_card(PNG, note)
        self.assertRegex(card["id"], r"\A[0-9a-f]{32}\Z")
        self.assertTrue(Path(card["image_path"]).is_absolute())
        self.assertEqual(Path(card["image_path"]).name, "marked.png")
        self.assertEqual(chat_card.load_card(card["id"]), (PNG, note))

        card_dir = Path(card["image_path"]).parent
        cards_dir = card_dir.parent
        self.assertEqual(stat.S_IMODE(cards_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(card_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(Path(card["image_path"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((card_dir / "note.txt").stat().st_mode), 0o600)
        self.assertEqual((card_dir / "note.txt").read_bytes(), note.encode("utf-8"))

    def test_unknown_expired_and_malformed_ids_cannot_read_a_card(self) -> None:
        card = chat_card.save_card(PNG, "note")
        with self.assertRaises(ValueError):
            chat_card.load_card("../" + card["id"])
        with self.assertRaises(ValueError):
            chat_card.load_card("A" + "0" * 31)
        with self.assertRaises(ValueError):
            chat_card.load_card("0" * 31)
        with self.assertRaises(ValueError):
            chat_card.load_card("f" * 32)

    def test_size_and_type_limits_are_checked_before_storage(self) -> None:
        with self.assertRaisesRegex(ValueError, "PNG"):
            chat_card.save_card(b"not png", "note")
        with self.assertRaisesRegex(ValueError, "12 MiB"):
            chat_card.save_card(chat_card.PNG_SIGNATURE + b"x" * chat_card.MAX_PNG_BYTES, "note")
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            chat_card.save_card(PNG, "é" * (chat_card.MAX_NOTE_BYTES // 2 + 1))
        with self.assertRaises(TypeError):
            chat_card.save_card(PNG, None)  # type: ignore[arg-type]

    def test_expired_card_becomes_unreadable_and_is_pruned_on_explicit_load(self) -> None:
        created_at = 1000.0
        with patch.object(chat_card.time, "time", return_value=created_at):
            card = chat_card.save_card(PNG, "temporary")
        card_dir = Path(card["image_path"]).parent
        with patch.object(chat_card.time, "time", return_value=created_at + chat_card.CARD_TTL_SECONDS):
            with self.assertRaisesRegex(ValueError, "unknown or expired"):
                chat_card.load_card(card["id"])
        self.assertFalse(card_dir.exists())

    def test_next_save_prunes_expired_cards_without_touching_a_live_card(self) -> None:
        created_at = 2000.0
        with patch.object(chat_card.time, "time", return_value=created_at):
            old = chat_card.save_card(PNG, "old")
        with patch.object(chat_card.time, "time", return_value=created_at + chat_card.CARD_TTL_SECONDS):
            fresh = chat_card.save_card(PNG, "new")
            self.assertEqual(chat_card.load_card(fresh["id"]), (PNG, "new"))
        self.assertFalse(Path(old["image_path"]).parent.exists())
        self.assertTrue(Path(fresh["image_path"]).parent.exists())

    def test_load_rejects_symlinked_card_file_without_reading_target(self) -> None:
        card = chat_card.save_card(PNG, "note")
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside secret")
        image_path = Path(card["image_path"])
        image_path.unlink()
        image_path.symlink_to(outside)
        with self.assertRaises(OSError):
            chat_card.load_card(card["id"])
        self.assertEqual(outside.read_bytes(), b"outside secret")

    def test_store_rejects_symlinked_managed_directory(self) -> None:
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        managed = self.runtime / "omarky"
        managed.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            chat_card.save_card(PNG, "note")
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_missing_xdg_runtime_uses_private_home_cache_fallback(self) -> None:
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=False):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            card = chat_card.save_card(PNG, "fallback")
            self.assertEqual(chat_card.load_card(card["id"]), (PNG, "fallback"))
        self.assertTrue(str(Path(card["image_path"])).startswith(str(self.home / ".cache")))
        self.assertEqual(stat.S_IMODE((self.home / ".cache" / "omarky").stat().st_mode), 0o700)

    def test_forget_cli_deletes_one_id_or_explicit_all_and_leaves_other_files(self) -> None:
        first = chat_card.save_card(PNG, "one")
        second = chat_card.save_card(PNG, "two")
        cards_dir = Path(first["image_path"]).parent.parent
        unrelated = cards_dir / "not-a-card"
        unrelated.mkdir(mode=0o700)
        keep = cards_dir / "keep.txt"
        keep.write_text("not managed", encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "bin" / "omarky-forget"
        env = {**os.environ, "XDG_RUNTIME_DIR": str(self.runtime), "HOME": str(self.home)}

        one = subprocess.run([sys.executable, str(script), first["id"]], env=env,
                             capture_output=True, text=True, check=False)
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertFalse(Path(first["image_path"]).parent.exists())
        self.assertTrue(Path(second["image_path"]).parent.exists())

        all_cards = subprocess.run([sys.executable, str(script), "--all"], env=env,
                                   capture_output=True, text=True, check=False)
        self.assertEqual(all_cards.returncode, 0, all_cards.stderr)
        self.assertIn("Removed 1 managed OMarky card", all_cards.stdout)
        self.assertFalse(Path(second["image_path"]).parent.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(keep.read_text(encoding="utf-8"), "not managed")

    def test_forget_cli_requires_one_explicit_scope(self) -> None:
        script = Path(__file__).resolve().parents[1] / "bin" / "omarky-forget"
        result = subprocess.run([sys.executable, str(script)], env=os.environ.copy(),
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("provide exactly one", result.stderr)


if __name__ == "__main__":
    unittest.main()
