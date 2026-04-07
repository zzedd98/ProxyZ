# -*- coding: utf-8 -*-
"""
Assistant graphique de mise a jour pour ProxyZ.
- Lance par ProxyZ : recoit les URLs en argument.
- Lance seul : utilise les constantes ci-dessous, ou les variables d'environnement / ligne de commande.

Packaging :
pyinstaller --noconfirm --onefile --windowed --name ProxyZUpdater ProxyZUpdater.py
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import ttk

UPDATE_GITHUB_REPO = "zzedd98/ProxyZ"
UPDATE_MANIFEST_URL = ""

_UA_GH = "ProxyZ-UpdateCheck/1.0"
_RE_GH_LATEST = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/releases/latest/download/([^/?#]+)$",
    re.I,
)


def _repo_from_gh_latest_url(url: str) -> str:
    m = _RE_GH_LATEST.match((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def _gh_headers_api() -> dict:
    return {
        "User-Agent": _UA_GH,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_headers_asset() -> dict:
    return {"User-Agent": _UA_GH}


def _gh_api_latest_release(owner: str, repo: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
        headers=_gh_headers_api(),
    )
    with urllib.request.urlopen(req, timeout=22.0) as resp:
        return json.loads(resp.read().decode("utf-8-sig", errors="replace"))


def _gh_find_asset_url(release: dict, filename: str) -> str:
    for a in release.get("assets") or []:
        if (a.get("name") or "") == filename:
            u = (a.get("browser_download_url") or "").strip()
            if u:
                return u
    raise FileNotFoundError(f"Asset {filename!r} introuvable dans la release.")


def _gh_http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=_gh_headers_asset())
    with urllib.request.urlopen(req, timeout=22.0) as resp:
        return json.loads(resp.read().decode("utf-8-sig", errors="replace"))


def fetch_update_manifest_dict(
    manifest_url: str, github_repo_fallback: str = ""
) -> dict:
    repo = (github_repo_fallback or "").strip().strip("/") or _repo_from_gh_latest_url(
        manifest_url or ""
    )
    req = urllib.request.Request(
        (manifest_url or "").strip(), headers={"User-Agent": _UA_GH}
    )
    try:
        with urllib.request.urlopen(req, timeout=18.0) as resp:
            return json.loads(resp.read().decode("utf-8-sig", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code not in (404, 403):
            raise
    if "/" not in repo:
        raise RuntimeError("Manifest introuvable : verifie UPDATE_GITHUB_REPO.")
    o, _, r = repo.partition("/")
    try:
        rel = _gh_api_latest_release(o, r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(
                f"GitHub : aucune release 'latest' sur {o}/{r} (404). "
                "Lance le workflow 'Build ProxyZ' sur GitHub."
            ) from e
        raise
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
        rq = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA_GH})
        with urllib.request.urlopen(rq, timeout=15.0) as resp:
            if resp.status < 400:
                return url
    except Exception:
        pass
    if "/" not in repo:
        return url
    o, _, r = repo.partition("/")
    rel = _gh_api_latest_release(o, r)
    return _gh_find_asset_url(rel, asset_filename)


def _updater_root_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _default_target_exe() -> str:
    return os.path.join(_updater_root_dir(), "ProxyZ.exe")


def _read_local_build_id(target_exe: str) -> str:
    d = os.path.dirname(os.path.abspath(target_exe))
    for fname in ("version.txt", "build_id.txt"):
        p = os.path.join(d, fname)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                line = (f.readline() or "").strip()
            if line:
                return line
        except Exception:
            continue
    return ""


def _write_local_build_id(target_exe: str, build_id: str) -> None:
    if not build_id:
        return
    d = os.path.dirname(os.path.abspath(target_exe))
    p = os.path.join(d, "version.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(build_id.strip() + "\n")


def _manifest_url_from_repo(repo: str) -> str:
    r = (repo or "").strip().strip("/")
    if not r:
        return ""
    return f"https://github.com/{r}/releases/latest/download/update-manifest.json"


def _download_to_file(url: str, dest_path: str, progress_q: queue.Queue) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ProxyZUpdater/1.0"})
    with urllib.request.urlopen(req, timeout=900) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        n = 0
        block = 256 * 1024
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(block)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
                progress_q.put(("progress", n, total))
    progress_q.put(("done", None, None))


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        parent,
        text,
        command,
        width=170,
        height=42,
        radius=14,
        bg="#0284C7",
        bg_hover="#0EA5E9",
        bg_disabled="#334155",
        fg="#FFFFFF",
        canvas_bg="#0B1220",
    ):
        super().__init__(
            parent,
            width=width,
            height=height,
            bd=0,
            highlightthickness=0,
            bg=canvas_bg,
        )
        self._text = text
        self._command = command
        self._radius = radius
        self._bg = bg
        self._bg_hover = bg_hover
        self._bg_disabled = bg_disabled
        self._fg = fg
        self._enabled = True
        self._shape_id = None
        self._text_id = None
        self._draw(self._bg)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _rounded_points(self, x1, y1, x2, y2, r):
        return [
            x1 + r,
            y1,
            x2 - r,
            y1,
            x2,
            y1,
            x2,
            y1 + r,
            x2,
            y2 - r,
            x2,
            y2,
            x2 - r,
            y2,
            x1 + r,
            y2,
            x1,
            y2,
            x1,
            y2 - r,
            x1,
            y1 + r,
            x1,
            y1,
        ]

    def _draw(self, color):
        self.delete("all")
        w = int(self.cget("width"))
        h = int(self.cget("height"))
        pts = self._rounded_points(2, 2, w - 2, h - 2, self._radius)
        self._shape_id = self.create_polygon(pts, smooth=True, fill=color, outline="")
        self._text_id = self.create_text(
            w // 2,
            h // 2,
            text=self._text,
            fill=self._fg,
            font=("Segoe UI", 10, "bold"),
        )

    def _on_enter(self, _e):
        if self._enabled:
            self._draw(self._bg_hover)

    def _on_leave(self, _e):
        if self._enabled:
            self._draw(self._bg)

    def _on_click(self, _e):
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self._draw(self._bg if self._enabled else self._bg_disabled)

    def set_text(self, text: str):
        self._text = str(text)
        self._draw(self._bg if self._enabled else self._bg_disabled)

    def set_command(self, command):
        self._command = command

    def set_palette(self, bg: str, bg_hover: str, bg_disabled: str = "#334155"):
        self._bg = bg
        self._bg_hover = bg_hover
        self._bg_disabled = bg_disabled
        self._draw(self._bg if self._enabled else self._bg_disabled)


def _replace_exe(target: str, tmp_dl: str) -> None:
    target = os.path.abspath(target)
    bak = target + ".old"
    target_existed = os.path.isfile(target)

    if not os.path.isfile(tmp_dl):
        raise RuntimeError("Fichier telecharge introuvable (installation interrompue).")
    if os.path.getsize(tmp_dl) <= 0:
        raise RuntimeError("Fichier telecharge vide (installation interrompue).")

    for _ in range(120):
        try:
            if os.path.isfile(bak):
                os.remove(bak)
            break
        except OSError:
            time.sleep(0.25)

    if target_existed:
        for _ in range(120):
            try:
                os.replace(target, bak)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("Impossible de remplacer l'executable (fichier verrouille ?).")

    installed = False
    for _ in range(120):
        try:
            os.replace(tmp_dl, target)
            installed = True
            break
        except OSError:
            time.sleep(0.25)

    if not installed:
        # Rollback best-effort : remettre l'ancien exe en place si on l'a sauvegarde.
        if os.path.isfile(bak):
            for _ in range(60):
                try:
                    if os.path.isfile(target):
                        os.remove(target)
                    os.replace(bak, target)
                    break
                except OSError:
                    time.sleep(0.25)
        raise RuntimeError("Impossible d'installer le nouveau fichier.")

    for _ in range(120):
        try:
            if os.path.isfile(bak):
                os.remove(bak)
            break
        except OSError:
            time.sleep(0.25)
    # Si .old reste verrouille (ancien process encore vivant), on ne bloque pas l'update.


class UpdaterApp:
    def __init__(
        self,
        target_exe: str,
        app_name: str,
        manifest_url: str,
        download_url: str,
        local_build_id: str,
        remote_build_id: str,
        github_repo_fallback: str = "",
    ):
        self.target_exe = os.path.abspath(target_exe)
        self.app_name = app_name or "ProxyZ"
        self.manifest_url = (manifest_url or "").strip()
        self.download_url = (download_url or "").strip()
        self.local_build_id = (local_build_id or "").strip()
        self.remote_build_id = (remote_build_id or "").strip()
        self._github_repo = (github_repo_fallback or "").strip().strip("/")

        self.root = tk.Tk()
        self.root.title(f"Mise a jour - {self.app_name}")
        self.root.resizable(False, False)
        self.root.minsize(520, 230)
        self.root.configure(bg="#0B1220")

        self._progress_q: queue.Queue = queue.Queue()

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("PZ.TFrame", background="#0B1220")
        style.configure(
            "PZTitle.TLabel",
            background="#0B1220",
            foreground="#E6F2FF",
            font=("Segoe UI", 17, "bold"),
        )
        style.configure(
            "PZInfo.TLabel",
            background="#0B1220",
            foreground="#93C5FD",
            font=("Segoe UI", 12),
        )
        style.configure(
            "PZ.Horizontal.TProgressbar",
            troughcolor="#1E293B",
            background="#38BDF8",
            bordercolor="#1E293B",
            lightcolor="#38BDF8",
            darkcolor="#0EA5E9",
        )
        frm = ttk.Frame(self.root, style="PZ.TFrame", padding=18)
        frm.pack(fill=tk.BOTH, expand=True)

        self.lbl_title = ttk.Label(frm, text="", style="PZTitle.TLabel")
        self.lbl_title.pack(anchor=tk.W)

        self.lbl_brief = ttk.Label(frm, text="", style="PZInfo.TLabel")
        self.lbl_brief.pack(anchor=tk.W, pady=(8, 8))

        self.progress = ttk.Progressbar(
            frm, mode="determinate", maximum=100, style="PZ.Horizontal.TProgressbar"
        )
        self.progress.pack(fill=tk.X, pady=(0, 8))
        self.progress.pack_forget()

        self.lbl_progress = ttk.Label(frm, text="", style="PZInfo.TLabel")
        self.lbl_progress.pack(anchor=tk.W)

        btn_row = ttk.Frame(frm, style="PZ.TFrame")
        btn_row.pack(pady=(10, 0))

        self.btn_install = RoundedButton(
            btn_row,
            text="UPDATE",
            command=self._on_install,
            width=230,
            height=54,
            radius=18,
            bg="#16A34A",
            bg_hover="#22C55E",
        )
        self.btn_install.pack()

        self._apply_state_from_args_or_fetch()

    def _set_body(self, text: str) -> None:
        line = (text or "").strip().splitlines()
        self.lbl_brief.configure(text=line[0] if line else "")

    def _resolve_manifest_url(self) -> str:
        if self.manifest_url:
            return self.manifest_url
        env_m = (os.environ.get("PROXYZ_UPDATE_MANIFEST_URL") or "").strip()
        if env_m:
            return env_m
        if (UPDATE_MANIFEST_URL or "").strip():
            return UPDATE_MANIFEST_URL.strip()
        repo_env = (os.environ.get("PROXYZ_GITHUB_REPO") or "").strip().strip("/")
        if repo_env:
            return _manifest_url_from_repo(repo_env)
        return _manifest_url_from_repo(UPDATE_GITHUB_REPO)

    def _is_target_exe_present(self) -> bool:
        try:
            return os.path.isfile(self.target_exe)
        except Exception:
            return False

    def _apply_state_from_args_or_fetch(self) -> None:
        if self.download_url and self.remote_build_id:
            try:
                self.download_url = resolve_latest_release_asset_url(
                    self.download_url, self._github_repo, "ProxyZ.exe"
                )
            except Exception:
                pass
            self._show_update_available_ui()
            return

        murl = self._resolve_manifest_url()
        if not murl:
            self.lbl_title.configure(text="Configuration incomplete")
            self._set_body("Configuration des mises a jour manquante.")
            self.btn_install.set_text("FERMER")
            self.btn_install.set_command(self.root.destroy)
            self.btn_install.set_palette("#334155", "#475569")
            self.btn_install.set_enabled(True)
            return

        self.manifest_url = murl
        try:
            man = fetch_update_manifest_dict(murl, self._github_repo)
            remote = str(man.get("build_id") or man.get("version") or "").strip()
            durl = resolve_latest_release_asset_url(
                str(man.get("download_url") or "").strip(),
                self._github_repo,
                "ProxyZ.exe",
            )
        except Exception as e:
            self.lbl_title.configure(text="Verification impossible")
            self._set_body(f"Impossible de joindre le serveur ({e}).")
            self.btn_install.set_text("FERMER")
            self.btn_install.set_command(self.root.destroy)
            self.btn_install.set_palette("#334155", "#475569")
            self.btn_install.set_enabled(True)
            return

        if not remote or not durl:
            self.lbl_title.configure(text="Manifest invalide")
            self._set_body("Manifest invalide.")
            self.btn_install.set_text("FERMER")
            self.btn_install.set_command(self.root.destroy)
            self.btn_install.set_palette("#334155", "#475569")
            self.btn_install.set_enabled(True)
            return

        self.remote_build_id = remote
        self.download_url = durl
        if not self.local_build_id:
            self.local_build_id = _read_local_build_id(self.target_exe)

        if not self._is_target_exe_present():
            self.lbl_title.configure(text="Application introuvable")
            self._set_body("ProxyZ.exe est introuvable. Cliquez UPDATE pour reinstaller.")
            self.btn_install.set_text("UPDATE")
            self.btn_install.set_command(self._on_install)
            self.btn_install.set_palette("#16A34A", "#22C55E")
            self.btn_install.set_enabled(True)
            return

        if not self.local_build_id:
            # Premiere installation legacy sans metadata locale:
            # on propose quand meme la mise a jour au lieu de bloquer.
            self._show_update_available_ui()
            self._set_body("Version locale introuvable. UPDATE recommande.")
            return

        if self.local_build_id == self.remote_build_id:
            self.lbl_title.configure(text="Logiciel a jour")
            self._set_body(f"{self.app_name} est a jour.")
            self.btn_install.set_text("FERMER")
            self.btn_install.set_command(self.root.destroy)
            self.btn_install.set_palette("#334155", "#475569")
            self.btn_install.set_enabled(True)
            return

        self._show_update_available_ui()

    def _show_update_available_ui(self) -> None:
        self.lbl_title.configure(text="Mise a jour disponible")
        self._set_body("Une mise a jour est disponible.")
        self.btn_install.set_text("UPDATE")
        self.btn_install.set_command(self._on_install)
        self.btn_install.set_palette("#16A34A", "#22C55E")
        self.btn_install.set_enabled(True)

    def _on_install(self) -> None:
        if not self.download_url:
            return
        self.btn_install.set_enabled(False)
        self.progress.pack(fill=tk.X, pady=(0, 8))
        self.progress["value"] = 0
        self.lbl_progress.configure(text="Preparation...")
        self.root.update_idletasks()

        threading.Thread(target=self._install_thread_main, daemon=True).start()
        self._poll_progress()

    def _install_thread_main(self) -> None:
        try:
            time.sleep(1.0)
            base = os.path.basename(self.target_exe)
            tmp_dl = os.path.join(os.path.dirname(self.target_exe), f".{base}.part")
            _download_to_file(self.download_url, tmp_dl, self._progress_q)
            self._progress_q.put(("replace", tmp_dl, None))
        except Exception as e:
            self._progress_q.put(("error", str(e), None))

    def _poll_progress(self) -> None:
        reschedule = True
        try:
            while True:
                try:
                    kind, a, b = self._progress_q.get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    n, total = a, b
                    if total and total > 0:
                        try:
                            self.progress.stop()
                        except Exception:
                            pass
                        self.progress.configure(mode="determinate", maximum=100)
                        pct = min(100, int(100 * n / total))
                        self.progress["value"] = pct
                        self.lbl_progress.configure(
                            text=f"Telechargement : {pct} % ({n // (1024 * 1024)} Mo)"
                        )
                    else:
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(12)
                        self.lbl_progress.configure(text="Telechargement en cours...")
                elif kind == "done":
                    pass
                elif kind == "replace":
                    tmp_dl = a
                    try:
                        try:
                            self.progress.stop()
                        except Exception:
                            pass
                        self.progress.configure(mode="determinate", maximum=100)
                        self.progress["value"] = 100
                        self.lbl_progress.configure(text="Installation...")
                        self.root.update_idletasks()
                        _replace_exe(self.target_exe, tmp_dl)
                        try:
                            _write_local_build_id(
                                self.target_exe, self.remote_build_id
                            )
                        except Exception:
                            pass
                        self.lbl_title.configure(text="Mise a jour terminee")
                        self._set_body("Mise a jour terminee. Relance manuelle de ProxyZ.")
                        self.lbl_progress.configure(text="")
                        self.btn_install.set_text("FERMER")
                        self.btn_install.set_command(self.root.destroy)
                        self.btn_install.set_enabled(True)
                        reschedule = False
                        return
                    except Exception as e:
                        try:
                            self.progress.stop()
                        except Exception:
                            pass
                        self._set_body(f"La mise a jour n'a pas pu etre finalisee ({e}).")
                        self.lbl_progress.configure(text="")
                        self.btn_install.set_text("FERMER")
                        self.btn_install.set_command(self.root.destroy)
                        self.btn_install.set_palette("#334155", "#475569")
                        self.btn_install.set_enabled(True)
                        reschedule = False
                        return
                elif kind == "error":
                    try:
                        self.progress.stop()
                    except Exception:
                        pass
                    try:
                        self.progress.pack_forget()
                    except Exception:
                        pass
                    self.lbl_progress.configure(text="")
                    self._set_body(f"La mise a jour a echoue ({a}).")
                    self.btn_install.set_text("FERMER")
                    self.btn_install.set_command(self.root.destroy)
                    self.btn_install.set_palette("#334155", "#475569")
                    self.btn_install.set_enabled(True)
                    reschedule = False
                    return
        finally:
            try:
                if reschedule and self.root.winfo_exists():
                    self.root.after(80, self._poll_progress)
            except Exception:
                pass


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Assistant de mise a jour ProxyZ")
    p.add_argument("--target-exe", default="", help="Chemin de ProxyZ.exe")
    p.add_argument("--download-url", default="", help="URL du nouvel exe (si deja connu)")
    p.add_argument("--manifest-url", default="", help="URL du update-manifest.json")
    p.add_argument("--github-repo", default="", help='Format "owner/repo"')
    p.add_argument("--local-build-id", default="", help="Build actuellement installe")
    p.add_argument("--remote-build-id", default="", help="Build publie a installer")
    p.add_argument("--app-display-name", default="ProxyZ", help="Nom affiche")
    return p.parse_args(argv)


def _github_repo_hint(args) -> str:
    r = (args.github_repo or "").strip().strip("/")
    if r:
        return r
    r = (os.environ.get("PROXYZ_GITHUB_REPO") or "").strip().strip("/")
    if r:
        return r
    return (UPDATE_GITHUB_REPO or "").strip().strip("/")


def main(argv=None):
    args = parse_args(argv)
    target = (args.target_exe or "").strip() or _default_target_exe()
    manifest_url = (args.manifest_url or "").strip()
    if not manifest_url and (args.github_repo or "").strip():
        manifest_url = _manifest_url_from_repo(args.github_repo)
    repo_hint = _github_repo_hint(args)

    app = UpdaterApp(
        target_exe=target,
        app_name=(args.app_display_name or "ProxyZ").strip(),
        manifest_url=manifest_url,
        download_url=(args.download_url or "").strip(),
        local_build_id=(args.local_build_id or "").strip(),
        remote_build_id=(args.remote_build_id or "").strip(),
        github_repo_fallback=repo_hint,
    )
    app.root.mainloop()


if __name__ == "__main__":
    main()
