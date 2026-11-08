"""Seed an offline profile without aliasing mutable state in the live home.

Only the committed app and PM generations are needed. Selection records, lock
files, generation roots and lease directories are private copies. Package
``lib/`` trees may be linked ONLY when every resolved entry is on a read-only
filesystem (not merely chmod'd). A plain ``check.sh`` therefore copies packages;
a read-only release namespace can safely share them. No hardlinks are used.

Enable the plugin through ``hermes config set plugins.enabled``: unlike plugin
admission, this does not explicitly re-resolve dependencies over the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

#: Hermes' own install, read-only. The seed is derived from it, never written.
HERMES_INSTALL = Path("/usr/local/lib/hermes-agent")


class SeedError(RuntimeError):
    """The dependency state could not be provided from local files."""


def install_key(install: Path = HERMES_INSTALL) -> str:
    """PM's key for a checkout: ``sha256(resolved path)[:16]`` (``pm.environments``)."""
    return hashlib.sha256(str(install.resolve()).encode("utf-8")).hexdigest()[:16]


def installed_state(install: Path = HERMES_INSTALL) -> Path:
    """The committed dependency state of the installed Hermes."""
    return Path.home() / ".hermes" / "installs" / install_key(install)


def _selection(state: Path) -> tuple[dict, Path, Path, dict, Path]:
    """Validate both committed generations before creating any destination."""
    state = state.resolve()
    data = json.loads((state / "facts.json").read_text(encoding="utf-8"))
    fact = data["packages"]["venv"]
    environment = Path(fact["environment"]).resolve()
    lock = Path(fact["resolved_lock"]).resolve()
    relative = environment.relative_to(state / "environments")
    if len(relative.parts) != 2 or relative.name != "venv":
        raise ValueError("app environment must be a generation's venv")
    lock.relative_to(environment.parent / "workspace")
    if not (environment / "pyvenv.cfg").is_file() or not lock.is_file():
        raise ValueError("incomplete app environment")
    selected = json.loads((state / "pm-runtime" / "selected.json").read_text(encoding="utf-8"))
    generation = Path(selected["generation"])
    if (
        generation.is_absolute()
        or len(generation.parts) != 2
        or generation.parts[0] != "generations"
    ):
        raise ValueError("invalid PM generation")
    pm_environment = (state / "pm-runtime" / generation).resolve()
    pm_environment.relative_to(state / "pm-runtime" / "generations")
    if (
        not (pm_environment / "pyvenv.cfg").is_file()
        or not (pm_environment / "pm-runtime.json").is_file()
    ):
        raise ValueError("incomplete PM environment")
    return data, environment, lock, selected, pm_environment


def available(install: Path = HERMES_INSTALL) -> bool:
    """Can this machine provide both committed generations locally?"""
    try:
        _selection(installed_state(install))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def native_home() -> Path:
    """The real Hermes profile (``~/.hermes``), resolved. Never a seed target."""
    return (Path.home() / ".hermes").resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class IsolationRefused(SeedError):
    """The seed would write into the real profile. Refused, never attempted."""


def assert_profile_isolated(profile: Path) -> Path:
    """Refuse a profile that is, or lives inside, the real ``~/.hermes``.

    Checked on the *resolved* path, so a symlinked parent cannot smuggle the
    seed back into real state. The profile must also be outside the real
    profile by construction: Hermes collapses a home nested under ``~/.hermes``
    back to ``~/.hermes`` (see ``tests/integration/test_r17_fresh_isolation.py``).
    """
    resolved = profile.resolve()
    real = native_home()
    if _inside(resolved, real) or resolved == real:
        raise IsolationRefused(
            f"refusing to seed {profile}: it resolves to {resolved}, inside the real profile {real}"
        )
    return resolved


def _read_only_tree(path: Path) -> bool:
    """Mode bits (even 0444) are not isolation, particularly when run as root.

    Check descendants as well: a writable bind or symlink below an otherwise
    read-only lib/ must force a copy. Fail closed on unsupported platforms.
    """
    readonly = getattr(os, "ST_RDONLY", 0)
    if not readonly or not hasattr(os, "statvfs"):
        return False
    pending = [path]
    seen: set[Path] = set()
    try:
        while pending:
            resolved = pending.pop().resolve(strict=True)
            if resolved in seen:
                continue
            seen.add(resolved)
            if not os.statvfs(resolved).f_flag & readonly:
                return False
            if resolved.is_dir():
                pending.extend(resolved.iterdir())
    except (OSError, RuntimeError):
        return False
    return True


def _copy_environment(source: Path, target: Path, *, workspace: Path, install: Path) -> None:
    """Keep the venv root/locks private; share only immutable package payloads."""
    target.mkdir(parents=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.name in {".leases", ".lock"}:
            continue
        if (
            child.name == "lib64"
            and child.is_symlink()
            and child.resolve() == (source / "lib").resolve()
        ):
            destination.symlink_to("lib", target_is_directory=True)
        elif child.name == "lib" and _read_only_tree(child) and _read_only_tree(workspace):
            destination.symlink_to(child.resolve(), target_is_directory=True)
        elif child.is_dir():
            shutil.copytree(
                child, destination, symlinks=False, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            shutil.copyfile(child, destination)
            shutil.copymode(child, destination)
    (target / ".lock").touch()
    # Editable imports must not keep pointing into a writable live workspace
    # when lib/ was copied. Immutable lib/ can keep its immutable references.
    if not (target / "lib").is_symlink():
        for pattern in ("*.pth", "__editable__*finder.py"):
            for file in (target / "lib").glob(f"python*/site-packages/{pattern}"):
                text = file.read_text(encoding="utf-8")
                rewritten = text.replace(str(workspace), str(install.resolve()))
                if rewritten != text:
                    file.write_text(rewritten, encoding="utf-8")


def seed_profile(
    profile: Path, *, install: Path = HERMES_INSTALL, source_state: Optional[Path] = None
) -> Optional[Path]:
    """Copy minimal committed state to a fresh profile, never the owner's locks.

    Missing/incomplete generations return ``None``; callers must fail, not sync
    online. Existing destinations are refused rather than trusted or removed.
    This also rejects a leftover install symlink from the old unsafe seed.
    """
    profile = assert_profile_isolated(profile)
    target = profile / "installs" / install_key(install)
    if target.parent.is_symlink() or target.is_symlink():
        raise IsolationRefused(f"refusing symlinked install state: {target}")
    assert_profile_isolated(target)
    if target.exists():
        raise SeedError(f"refusing existing seed {target}; use a fresh profile")
    source = (source_state or installed_state(install)).resolve()
    try:
        data, environment, lock, selected, pm_environment = _selection(source)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    local_environment = target / environment.relative_to(source)
    local_lock = target / lock.relative_to(source)
    local_pm = target / "pm-runtime" / selected["generation"]
    try:
        _copy_environment(
            environment,
            local_environment,
            workspace=environment.parent / "workspace",
            install=install,
        )
        local_lock.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(lock, local_lock)
        (local_environment.parent / ".lease-managed").touch()
        (local_environment.parent / ".leases").mkdir()
        _copy_environment(
            pm_environment, local_pm, workspace=environment.parent / "workspace", install=install
        )
        (local_pm / ".lease-managed").touch()
        (local_pm / ".leases").mkdir()
        (target / ".install.lock").touch()
        (target / "pm-runtime" / ".prepare.lock").touch()
        (target / "pm-runtime" / "selected.json").write_text(json.dumps(selected), encoding="utf-8")
        fact = data["packages"]["venv"]
        fact["environment"] = str(local_environment)
        fact["resolved_lock"] = str(local_lock)
        # Publish the selection last, after all inputs exist.
        (target / "facts.json").write_text(json.dumps(data), encoding="utf-8")
    except (OSError, ValueError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise SeedError(f"cannot copy offline dependency state: {exc}") from exc
    return target


def main(argv: list[str]) -> int:
    """``--check``: report whether the state can be provided (exit 0/1).

    ``check.sh`` calls this before the e2e so a machine that cannot prepare its
    own inputs fails the gate loudly instead of falling back to a network sync.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report seed availability")
    parser.add_argument("--seed", type=Path, help="seed this profile directory")
    options = parser.parse_args(argv)
    if options.check:
        if available():
            print(f"offline dependency state available from {installed_state()}")
            return 0
        print(
            f"no offline dependency state: {installed_state()} has no committed "
            "generation (run `hermes pm install` on the installed Hermes)",
            file=sys.stderr,
        )
        return 1
    if options.seed is not None:
        target = seed_profile(options.seed)
        if target is None:
            print("no offline dependency state available", file=sys.stderr)
            return 1
        print(target)
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
