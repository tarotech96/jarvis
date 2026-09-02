"""
code.py - an inventory of the source files sitting beside the notes.

vault.py indexes .md/.txt/.pdf, which is right for a knowledge graph and
useless for "how many controllers are in hanasee-backend": JARVIS had never
read a single .ts file, so it correctly answered that it couldn't know.

Loading 3,000 source files into memory to fix that would be the wrong
trade - 27 MB of content nobody asked for, and a graph swamped by nodes.
So this keeps PATHS only, which is cheap and is already enough to count and
list things, and reads a file's text off disk on demand when the question
actually needs what's inside.

Read-only, like everything else here. Nothing in this file writes.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Importable as agent.code and runnable as `python3 agent/code.py`, the way
# vault.py is - which needs the project root on the path before the import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import vault  # noqa: E402

CODE_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".py", ".rb", ".php", ".go", ".rs", ".java", ".kt", ".cs", ".swift",
    ".dart", ".c", ".h", ".cpp", ".hpp", ".m", ".mm", ".scala", ".ex", ".exs",
    ".sql", ".prisma", ".graphql", ".proto",
    ".yml", ".yaml", ".toml", ".ini", ".env.example",
    ".sh", ".bash", ".zsh", ".ps1", ".dockerfile",
    ".html", ".css", ".scss", ".sass", ".less",
}

# Config noise that would otherwise dominate any path-token search.
SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock",
    "poetry.lock", "Cargo.lock", "Gemfile.lock",
}

MAX_READ_BYTES = 24_000
_SPLIT_RE = re.compile(r"[^0-9a-z]+")


def _path_tokens(rel: str) -> set:
    return {t for t in _SPLIT_RE.split(rel.lower()) if len(t) > 1}


@dataclass
class CodeFile:
    path: Path
    rel: str            # relative to the configured root
    repo: str           # nearest git repository, or the top folder
    name: str
    size: int
    tokens: set = field(default_factory=set)


@dataclass
class CodeIndex:
    files: list = field(default_factory=list)

    def repos(self) -> dict:
        out: dict[str, int] = {}
        for f in self.files:
            out[f.repo] = out.get(f.repo, 0) + 1
        return out

    def search(self, query_tokens: Iterable[str], repos: Iterable[str] | None = None,
               limit: int = 40) -> list:
        """
        Ranks files by how much of the question their PATH accounts for.

        Path-only is a deliberate limit: it answers "which files are the
        controllers" precisely and cheaply, and it never pretends to know
        what is inside them - that is what read() is for.
        """
        wanted = {t for t in query_tokens if len(t) > 1}
        if not wanted:
            return []
        repo_filter = set(repos) if repos else None

        scored = []
        for f in self.files:
            if repo_filter and f.repo not in repo_filter:
                continue
            overlap = wanted & f.tokens
            if not overlap:
                continue
            # A hit in the filename beats one in some parent directory.
            name_tokens = _path_tokens(f.name)
            score = len(overlap) + len(overlap & name_tokens)
            scored.append((score, f))
        scored.sort(key=lambda sf: (-sf[0], sf[1].rel))
        return [f for _s, f in scored[:limit]]

    def read(self, path: Path, max_bytes: int = MAX_READ_BYTES) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                return fh.read(max_bytes)
        except OSError:
            return ""


def build_index(folders: Iterable[Path]) -> CodeIndex:
    index = CodeIndex()
    for folder in folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in vault.SKIP_DIR_NAMES)
            here = Path(dirpath)
            for name in sorted(filenames):
                if name in SKIP_NAMES:
                    continue
                suffix = Path(name).suffix.lower()
                if suffix not in CODE_SUFFIXES:
                    continue
                path = here / name
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                repo_dir = vault._find_repo_root(path, root)
                rel = str(path.relative_to(root))
                index.files.append(CodeFile(
                    path=path,
                    rel=rel,
                    repo=(repo_dir or root).name,
                    name=name,
                    size=size,
                    tokens=_path_tokens(rel),
                ))
    return index


if __name__ == "__main__":
    from agent.data import VAULT_FOLDERS

    idx = build_index(VAULT_FOLDERS)
    print(f"{len(idx.files)} source files")
    for repo, n in sorted(idx.repos().items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {n:>5}  {repo}")
