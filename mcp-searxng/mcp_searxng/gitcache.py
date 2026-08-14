"""git 托管站仓库本地化：URL 判定、clone/pull、本地分析（避免托管站限流）。"""

import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from .config import ServerConfig
from .markdown import PageProcessor

class GitRepoCache:
    """git 托管站仓库本地化：URL 判定、clone/pull、本地分析（避免托管站限流）。"""

    _MARKERS = ("blob", "raw", "tree", "src")
    _README_NAMES = (
        "README.md",
        "readme.md",
        "README.rst",
        "readme.rst",
        "README.txt",
        "readme.txt",
        "README",
        "readme",
    )

    def __init__(self, config: ServerConfig):
        self._config = config
        self._directory = config.git_cache_dir
        self._directory.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @staticmethod
    def parse_repo_url(url: str) -> tuple[str, str, str, str, list[str]] | None:
        """解析 git 仓库内容 URL。

        返回 (host, owner, repo, kind, segs)，segs 为标记段之后的路径段
        （ref 在前，文件/目录路径在后，可能含 '/'）。
        非仓库内容 URL（issues/pulls/releases 等）返回 None。
        kind ∈ {"root", "blob", "raw", "tree"}。
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        path = parsed.path
        if not host or not path:
            return None
        if host not in ServerConfig.git_hosts:
            return None
        if path.endswith(".git"):
            path = path[:-4]
        segs = [s for s in path.split("/") if s]
        if len(segs) < 2:
            return None
        if host == "raw.githubusercontent.com":
            return host, segs[0], segs[1], "raw", segs[2:]
        owner, repo = segs[0], segs[1]
        rest = segs[2:]
        if not rest:
            return host, owner, repo, "root", []
        if rest[0] == "-":  # gitlab 的 /-/blob|raw|tree/ref/path
            rest = rest[1:]
        if rest and rest[0] in GitRepoCache._MARKERS:
            kind = "tree" if rest[0] == "src" else rest[0]
            return host, owner, repo, kind, rest[1:]
        return None

    @staticmethod
    def sanitize_name(name: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", name))

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _run_git(self, args: list[str], timeout: float) -> bool:
        """直连（含全局 git config）→ 各代理候选依次尝试，任一成功即返回 True。"""
        attempts = [(None, "直连")] + [
            (proxy, f"代理 {proxy}") for proxy in self._config.proxy_candidates
        ]
        errors = []
        for proxy, label in attempts:
            cmd = ["-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}"] + args
            if not proxy:
                cmd = args
            try:
                proc = subprocess.run(
                    ["git", *cmd], capture_output=True, text=True, timeout=timeout
                )
                if proc.returncode == 0:
                    return True
                errors.append(f"{label}: {proc.stderr.strip()[-200:]}")
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                errors.append(f"{label}: {e}")
        return False

    def _git_lines(self, repo_dir: Path, args: list[str]) -> list[str] | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_dir), *args],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.splitlines()

    def ensure_updated(self, host: str, owner: str, repo: str) -> tuple[Path | None, bool]:
        """仓库不存在则全量 clone，已存在则 pull --ff-only。

        返回 (repo_dir | None, updated)：updated 表示本地副本已更新到最新；
        仓库目录可用但更新失败时返回 (repo_dir, False)。
        """
        if not (self.sanitize_name(owner) and self.sanitize_name(repo)):
            return None, False
        repo_dir = self._directory / owner / repo
        clone_url = f"https://{host}/{owner}/{repo}.git"
        with self._lock_for(f"{owner}/{repo}"):
            try:
                if not (repo_dir / ".git").is_dir():
                    ok = self._run_git(
                        ["clone", clone_url, str(repo_dir)],
                        self._config.git_clone_timeout,
                    )
                    if not ok:
                        return None, False
                    return repo_dir, True
                ok = self._run_git(
                    ["-C", str(repo_dir), "pull", "--ff-only"],
                    self._config.git_pull_timeout,
                )
                return repo_dir, ok
            except Exception:
                return None, False

    def analyze(self, url: str) -> dict | None:
        """git 仓库内容 URL → 本地分析 entry（与 get_page 同构）；非 git 或失败返回 None。"""
        parsed = self.parse_repo_url(url)
        if parsed is None:
            return None
        host, owner, repo, kind, segs = parsed
        repo_dir, updated = self.ensure_updated(host, owner, repo)
        if repo_dir is None:
            return None
        try:
            if kind in ("blob", "raw"):
                md = self._render_file(repo_dir, segs)
            elif kind == "tree":
                md = self._render_tree(repo_dir, segs)
            else:
                md = self._render_root(repo_dir)
        except Exception:
            return None
        if not md:
            return None
        if not updated:
            md = f"{md}\n\n（仓库更新失败，内容可能过期）"
        return {
            "markdown": md,
            "title": f"{owner}/{repo}",
            "final_url": url,
            "fetched_at": time.time(),
        }

    def _file_tree(self, repo_dir: Path, prefix: str) -> list[str] | None:
        args = ["ls-tree", "-r", "--name-only", "HEAD"]
        if prefix:
            args.append(prefix)
        lines = self._git_lines(repo_dir, args)
        if lines is None:
            return None
        if prefix:
            lines = [ln for ln in lines if ln.startswith(prefix)]
        return lines

    def _render_root(self, repo_dir: Path) -> str | None:
        branch = self._git_lines(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
        branch_name = branch[0] if branch else "?"
        raw = self._file_tree(repo_dir, "")
        if raw is None:
            return None
        tree = raw[: self._config.git_tree_max_entries]
        parts = [
            f"仓库: {repo_dir.parent.name}/{repo_dir.name}（默认分支 {branch_name}）",
            "",
            f"📂 文件树（{len(raw)} 个文件）:",
        ]
        parts.extend(tree or ["（空仓库）"])
        if len(raw) > len(tree):
            parts.append(f"…（仅列前 {len(tree)} 条）")
        readme = self._read_readme(repo_dir)
        parts.append("")
        if readme is not None:
            parts.append("README:")
            parts.append(readme)
        else:
            parts.append("（仓库根无 README）")
        return "\n".join(parts)

    def _render_tree(self, repo_dir: Path, segs: list[str]) -> str | None:
        rel = "/".join(segs[1:]) if segs else ""
        prefix = rel + "/" if rel else ""
        args = ["ls-tree", "-r", "--name-only", "HEAD"]
        if prefix:
            args.append(prefix)
        raw = self._git_lines(repo_dir, args)
        if raw is None:
            return None
        if prefix:
            raw = [ln for ln in raw if ln.startswith(prefix)]
        tree = raw[: self._config.git_tree_max_entries]
        parts = [f"仓库: {repo_dir.parent.name}/{repo_dir.name}", ""]
        if rel:
            parts.append(f"📂 目录 {rel}/（{len(raw)} 个文件）:")
        else:
            parts.append(f"📂 文件树（{len(raw)} 个文件）:")
        parts.extend(tree or ["（空目录）"])
        if len(raw) > len(tree):
            parts.append(f"…（仅列前 {len(tree)} 条）")
        return "\n".join(parts)

    def _render_file(self, repo_dir: Path, segs: list[str]) -> str | None:
        """segs 前半为 ref（可能含 '/'），后半为文件路径；逐段扩展 ref 匹配。"""
        root = repo_dir.resolve()
        for i in range(len(segs), -1, -1):
            rel = "/".join(segs[i:])
            if not rel:
                continue
            try:
                target = (repo_dir / rel).resolve()
            except OSError:
                continue
            if target.is_relative_to(root) and target.is_file():
                break
        else:
            return None
        raw = self._read_local_file(repo_dir, str(target.relative_to(root)))
        if raw is None:
            return None
        if self._is_binary(raw):
            return f"文件: {target.relative_to(root)}（二进制，{len(raw)} 字节）\n\n[二进制内容不可展示]"
        body, truncated = PageProcessor.truncate_md(raw, self._config.git_file_chars)
        parts = [f"文件: {target.relative_to(root)}（{len(raw)} 字符）", ""]
        parts.append(body if body else "（空文件）")
        if truncated:
            parts.append(f"\n[已截断至 {self._config.git_file_chars} 字符]")
        return "\n".join(parts)

    def _read_local_file(self, repo_dir: Path, rel: str) -> str | None:
        try:
            root = repo_dir.resolve()
            target = (repo_dir / rel).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return None
            if target.stat().st_size > self._config.max_body_bytes:
                return None
            return target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None

    @staticmethod
    def _is_binary(raw: str) -> bool:
        return "\x00" in raw[:8192]

    def _read_readme(self, repo_dir: Path) -> str | None:
        for name in self._README_NAMES:
            raw = self._read_local_file(repo_dir, name)
            if raw is not None:
                body = raw[: self._config.git_readme_chars]
                if len(raw) > len(body):
                    body += "\n…（README 较长，已截断）"
                return body
        return None

