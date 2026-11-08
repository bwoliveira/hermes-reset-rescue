"""T-52 — the tree check must fail on a planted payload.

Catalog rule 17: a plugin ships source only. `scripts/check_tree.sh` reads the
git index (untracked `__pycache__` is not shipped content) and rejects
bytecode, binaries, archives, oversized files and escaping symlinks.

These tests build a throw-away repository so the failure modes are proven
without putting a binary in this one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECK = REPO / "scripts" / "check_tree.sh"

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import offline_guard  # noqa: E402  (path set above)

_ALLOWED = None


def setUpModule():
    global _ALLOWED
    _ALLOWED = offline_guard.allow_subprocess("bash", "git", "file")
    _ALLOWED.__enter__()


def tearDownModule():
    if _ALLOWED is not None:
        _ALLOWED.__exit__(None, None, None)


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Reset Rescue",
    "GIT_AUTHOR_EMAIL": "dev@example.invalid",
    "GIT_COMMITTER_NAME": "Reset Rescue",
    "GIT_COMMITTER_EMAIL": "dev@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=dict(os.environ, **GIT_ENV),
    )
    return done.stdout


class TreeCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rr-tree-")
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# clean\n", encoding="utf-8")
        (self.repo / "module.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "clean tree")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(CHECK), "--repo", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _track(self, name: str, data: bytes = b"") -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        _git(self.repo, "add", "-f", name)

    def test_clean_tree_passes(self):
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("RESULT: OK", done.stdout)

    def test_planted_pyc_fails(self):
        # Built from ints, not \xHH escapes: the install scanner reads three
        # hex escapes on one line as an obfuscated payload, and this file is
        # not one.
        self._track("pkg/__pycache__/module.cpython-313.pyc", bytes([0, 1, 2]) + b"bytecode")
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("binary or generated file tracked", done.stdout)
        self.assertIn("RESULT: FAIL", done.stdout)

    def test_loose_pyo_fails(self):
        self._track("module.pyo", bytes([0]))
        self.assertEqual(self._run().returncode, 1)

    def test_archive_fails(self):
        self._track("bundle.zip", b"PK" + bytes([3, 4]))
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("bundle.zip", done.stdout)

    def test_binary_disguised_as_python_fails(self):
        """`file` catches an ELF that claims to be source."""
        elf_header = bytes([0x7F]) + b"ELF" + bytes([2, 1, 1, 0])
        self._track("sneaky.py", elf_header + bytes(64))
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("binary content", done.stdout)

    def test_oversized_file_fails(self):
        self._track("huge.txt", b"a" * (600 * 1024))
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("limit", done.stdout)

    def test_os_junk_fails(self):
        self._track(".DS_Store", bytes(2))
        self.assertEqual(self._run().returncode, 1)

    def test_escaping_symlink_fails(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        link = self.repo / "link.txt"
        link.symlink_to(outside)
        _git(self.repo, "add", "link.txt")
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("symlink escapes", done.stdout)

    def test_inside_symlink_is_allowed(self):
        (self.repo / "target.md").write_text("x\n", encoding="utf-8")
        (self.repo / "alias.md").symlink_to(self.repo / "target.md")
        _git(self.repo, "add", "target.md", "alias.md")
        self.assertEqual(self._run().returncode, 0)

    def test_untracked_bytecode_is_ignored(self):
        """A local test run's __pycache__ is not shipped content."""
        (self.repo / "__pycache__").mkdir()
        (self.repo / "__pycache__" / "x.pyc").write_bytes(bytes([0]))
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_staged_mode_checks_the_index(self):
        self._track("planted.png", bytes([0x89]) + b"PNG")
        done = self._run("--staged")
        self.assertEqual(done.returncode, 1)
        self.assertIn("planted.png", done.stdout)

    # The first reviewed binary exception: docs/screenshots/*.png (T-65).
    PNG = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A]) + bytes(32)

    def test_screenshot_png_is_allowed(self):
        self._track("docs/screenshots/accounts-en-dark.png", self.PNG)
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_png_outside_screenshots_still_fails(self):
        self._track("docs/accounts.png", self.PNG)
        self.assertEqual(self._run().returncode, 1)

    def test_nested_screenshot_dir_fails(self):
        self._track("docs/screenshots/x/shot.png", self.PNG)
        self.assertEqual(self._run().returncode, 1)

    def test_other_binary_in_screenshots_fails(self):
        self._track("docs/screenshots/bundle.zip", b"PK" + bytes([3, 4]))
        self.assertEqual(self._run().returncode, 1)

    def test_fake_png_in_screenshots_fails(self):
        elf_header = bytes([0x7F]) + b"ELF" + bytes([2, 1, 1, 0])
        self._track("docs/screenshots/shot.png", elf_header + bytes(32))
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("not a PNG", done.stdout)

    def test_oversized_screenshot_fails(self):
        self._track("docs/screenshots/big.png", self.PNG + bytes(600 * 1024))
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("limit", done.stdout)

    def test_more_than_six_screenshots_fails(self):
        for i in range(7):
            self._track(f"docs/screenshots/s{i}.png", self.PNG)
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("more than 6 screenshots", done.stdout)

    # The second reviewed exception: README art in docs/art/ (PNG, JPEG, WebP).
    JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes(32)
    WEBP = b"RIFF" + bytes([0x20, 0, 0, 0]) + b"WEBP" + b"VP8 " + bytes(32)

    def test_art_png_jpeg_and_webp_are_allowed(self):
        self._track("docs/art/hero.png", self.PNG)
        self._track("docs/art/card.jpg", self.JPEG)
        self._track("docs/art/strip.jpeg", self.JPEG)
        self._track("docs/art/badge.webp", self.WEBP)
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_jpeg_outside_art_still_fails(self):
        self._track("docs/hero.jpg", self.JPEG)
        self.assertEqual(self._run().returncode, 1)

    def test_jpeg_in_screenshots_still_fails(self):
        self._track("docs/screenshots/shot.jpg", self.JPEG)
        self.assertEqual(self._run().returncode, 1)

    def test_nested_art_dir_fails(self):
        self._track("docs/art/x/hero.jpg", self.JPEG)
        self.assertEqual(self._run().returncode, 1)

    def test_other_binary_in_art_fails(self):
        self._track("docs/art/anim.gif", b"GIF89a" + bytes(32))
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("binary or generated file tracked", done.stdout)

    def test_fake_images_in_art_fail(self):
        elf_header = bytes([0x7F]) + b"ELF" + bytes([2, 1, 1, 0])
        for name in ("docs/art/a.png", "docs/art/b.jpg", "docs/art/c.webp"):
            self._track(name, elf_header + bytes(32))
        # A PNG renamed to .jpg is not a JPEG either.
        self._track("docs/art/d.jpg", self.PNG)
        done = self._run()
        self.assertEqual(done.returncode, 1)
        for name in ("a.png", "b.jpg", "c.webp", "d.jpg"):
            self.assertIn(name, done.stdout)
        self.assertIn("not a jpg image", done.stdout)

    def test_riff_that_is_not_webp_fails(self):
        self._track("docs/art/sound.webp", b"RIFF" + bytes([0x20, 0, 0, 0]) + b"WAVE" + bytes(32))
        self.assertEqual(self._run().returncode, 1)

    def test_oversized_art_fails(self):
        self._track("docs/art/big.jpg", self.JPEG + bytes(600 * 1024))
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("limit", done.stdout)

    def test_more_than_six_art_images_fails(self):
        for i in range(7):
            self._track(f"docs/art/a{i}.jpg", self.JPEG)
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertIn("more than 6 images in docs/art", done.stdout)

    def test_art_and_screenshot_caps_are_separate(self):
        for i in range(6):
            self._track(f"docs/art/a{i}.jpg", self.JPEG)
            self._track(f"docs/screenshots/s{i}.png", self.PNG)
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_missing_repo_is_a_usage_error(self):
        done = subprocess.run(
            ["bash", str(CHECK), "--repo", str(Path(self.tmp.name) / "nope")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 2)
        self.assertIn("not a git checkout", done.stderr)

    def test_this_repository_is_clean(self):
        done = subprocess.run(["bash", str(CHECK)], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("RESULT: OK", done.stdout)


if __name__ == "__main__":
    unittest.main()
