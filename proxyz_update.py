# -*- coding: utf-8 -*-
"""Verification de mise a jour ProxyZ et lancement de ProxyZUpdater."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

UPDATE_GITHUB_REPO = "zzedd98/ProxyZ"
_UA = "ProxyZ-UpdateCheck/1.0"
_RE_GH_LATEST = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/releases/latest/download/([^/?#]+)$",
    re.I,
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith(
    "win"
) else 0


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _read_embedded_build_id() -> str:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "version.txt")
    candidates.append(_app_dir() / "version.txt")
    for path in candidates:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8", errors="replace").strip()
                if value:
                    return value
        except Exception:
            continue
    return ""


def _read_local_build_id() -> str:
    app_dir = _app_dir()
    for fname in ("version.txt", "build_id.txt"):
        path = app_dir / fname
        if not path.is_file():
            continue
        try:
            line = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line and line[0].strip():
                return line[0].strip()
        except Exception:
            continue
    return _read_embedded_build_id()


def _repo_from_gh_latest_url(url: str) -> str:
    match = _RE_GH_LATEST.match((url or "").strip())
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def _manifest_url_from_repo(repo: str) -> str:
    cleaned = (repo or "").strip().strip("/")
    if not cleaned:
        return ""
    return f"https://github.com/{cleaned}/releases/latest/download/update-manifest.json"


def _resolve_manifest_url() -> tuple[str, str]:
    env_manifest = (os.environ.get("PROXYZ_UPDATE_MANIFEST_URL") or "").strip()
    if env_manifest:
        repo = (
            (os.environ.get("PROXYZ_GITHUB_REPO") or "").strip().strip("/")
            or _repo_from_gh_latest_url(env_manifest)
            or UPDATE_GITHUB_REPO
        )
        return env_manifest, repo

    repo = (
        (os.environ.get("PROXYZ_GITHUB_REPO") or "").strip().strip("/")
        or UPDATE_GITHUB_REPO
    )
    return _manifest_url_from_repo(repo), repo


def _gh_headers_api() -> dict:
    return {
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_api_latest_release(owner: str, repo: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
        headers=_gh_headers_api(),
    )
    with urllib.request.urlopen(req, timeout=22.0) as resp:
        return json.loads(resp.read().decode("utf-8-sig", errors="replace"))


def _gh_find_asset_url(release: dict, filename: str) -> str:
    for asset in release.get("assets") or []:
        if (asset.get("name") or "") == filename:
            url = (asset.get("browser_download_url") or "").strip()
            if url:
                return url
    raise FileNotFoundError(f"Asset {filename!r} introuvable dans la release.")


def _gh_http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=22.0) as resp:
        return json.loads(resp.read().decode("utf-8-sig", errors="replace"))


def fetch_update_manifest_dict(
    manifest_url: str, github_repo_fallback: str = ""
) -> dict:
    repo = (github_repo_fallback or "").strip().strip("/") or _repo_from_gh_latest_url(
        manifest_url or ""
    )
    req = urllib.request.Request(
        (manifest_url or "").strip(), headers={"User-Agent": _UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=18.0) as resp:
            return json.loads(resp.read().decode("utf-8-sig", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 403):
            raise
    if "/" not in repo:
        raise RuntimeError("Manifest introuvable : verifie UPDATE_GITHUB_REPO.")
    owner, _, name = repo.partition("/")
    rel = _gh_api_latest_release(owner, name)
    return _gh_http_json(_gh_find_asset_url(rel, "update-manifest.json"))


def resolve_latest_release_asset_url(
    asset_url: str, github_repo_fallback: str, asset_filename: str
) -> str:
    repo = (github_repo_fallback or "").strip().strip("/") or _repo_from_gh_latest_url(
        asset_url or ""
    )
    url = (asset_url or "").strip()
    if not url or "github.com" not in url.lower() or "/releases/" not in url.lower():
        return url
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            if resp.status < 400:
                return url
    except Exception:
        pass
    if "/" not in repo:
        return url
    owner, _, name = repo.partition("/")
    rel = _gh_api_latest_release(owner, name)
    return _gh_find_asset_url(rel, asset_filename)


@dataclass(frozen=True)
class ProxyZUpdateInfo:
    local_build_id: str
    remote_build_id: str
    download_url: str
    manifest_url: str
    github_repo: str


def is_update_check_enabled() -> bool:
    if (os.environ.get("PROXYZ_SKIP_UPDATE_CHECK") or "").strip() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if (os.environ.get("PROXYZ_UPDATER_CHILD") or "").strip() in ("1", "true", "yes"):
        return False
    return True


def _updater_paths() -> tuple[Optional[Path], Optional[Path]]:
    app_dir = _app_dir()
    exe = app_dir / "ProxyZUpdater.exe"
    script = app_dir / "ProxyZUpdater.py"
    return (exe if exe.is_file() else None, script if script.is_file() else None)


def check_proxyz_update() -> Optional[ProxyZUpdateInfo]:
    """
    Compare la version locale au manifest distant.
    Retourne ProxyZUpdateInfo si une mise a jour est disponible, sinon None.
    """
    manifest_url, github_repo = _resolve_manifest_url()
    if not manifest_url:
        return None

    try:
        manifest = fetch_update_manifest_dict(manifest_url, github_repo)
        remote_build_id = str(
            manifest.get("build_id") or manifest.get("version") or ""
        ).strip()
        download_url = resolve_latest_release_asset_url(
            str(manifest.get("download_url") or "").strip(),
            github_repo,
            "ProxyZ.exe",
        )
    except Exception as exc:
        print(f"[UPDATE] Verification impossible : {exc}")
        return None

    if not remote_build_id or not download_url:
        print("[UPDATE] Manifest distant invalide.")
        return None

    local_build_id = _read_local_build_id()
    if local_build_id and local_build_id == remote_build_id:
        print(f"[UPDATE] {local_build_id} — logiciel a jour.")
        return None

    print(
        f"[UPDATE] Mise a jour disponible "
        f"(local={local_build_id or '?'} -> distant={remote_build_id})."
    )
    return ProxyZUpdateInfo(
        local_build_id=local_build_id,
        remote_build_id=remote_build_id,
        download_url=download_url,
        manifest_url=manifest_url,
        github_repo=github_repo,
    )


def launch_proxyz_updater(info: ProxyZUpdateInfo) -> bool:
    """Lance ProxyZUpdater.exe (ou .py) avec les URLs deja resolues."""
    updater_exe, updater_py = _updater_paths()
    if updater_exe is None and updater_py is None:
        print(
            "[UPDATE] ProxyZUpdater introuvable a cote de ProxyZ "
            "(ProxyZUpdater.exe ou ProxyZUpdater.py)."
        )
        return False

    app_dir = _app_dir()
    target_exe = app_dir / "ProxyZ.exe"
    args = [
        "--target-exe",
        str(target_exe),
        "--manifest-url",
        info.manifest_url,
        "--download-url",
        info.download_url,
        "--github-repo",
        info.github_repo,
        "--local-build-id",
        info.local_build_id,
        "--remote-build-id",
        info.remote_build_id,
        "--app-display-name",
        "ProxyZ",
    ]

    if updater_exe is not None:
        cmd = [str(updater_exe), *args]
    else:
        cmd = [sys.executable, str(updater_py), *args]

    try:
        subprocess.Popen(
            cmd,
            cwd=str(app_dir),
            close_fds=not sys.platform.startswith("win"),
        )
        print("[UPDATE] ProxyZUpdater lance.")
        return True
    except Exception as exc:
        print(f"[UPDATE] Impossible de lancer ProxyZUpdater : {exc}")
        return False


def schedule_proxyz_update_check(on_update_found: Optional[Callable[[], None]] = None) -> None:
    """
    Verifie la version en arriere-plan (thread daemon).
    Lance ProxyZUpdater automatiquement si une mise a jour est disponible.
    """

    def _worker() -> None:
        if not is_update_check_enabled():
            return
        info = check_proxyz_update()
        if info is None:
            return
        if launch_proxyz_updater(info) and on_update_found:
            try:
                on_update_found()
            except Exception:
                pass

    threading.Thread(target=_worker, name="ProxyZUpdateCheck", daemon=True).start()
