"""
vault.py - turns a set of folders into a searchable graph.

Walks the configured folders read-only, indexes .md / .txt / .pdf files.
Nothing in this file writes anywhere; it only reads.

Three kinds of edge, all of them facts about the files themselves - none
of them guessed from "these look similar":

  wikilink  [[Other Note]] resolved by title. How a notes vault links.
  link      [a doc](../other.md) resolved by relative path. How repo docs
            link, which is the only kind a real codebase actually has.
  repo      "these two files live in the same repository", drawn from each
            doc to that repo's README. Weakest of the three, and rendered
            faintest, but without it a folder of repo docs has no edges at
            all and the graph is just dust.

Run standalone for an index report:

    python3 agent/vault.py
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

# Dependency, build and tooling output. Pointing the vault at real repos
# instead of a notes folder means most of what rglob finds is vendored
# markdown nobody wrote - licences from site-packages, an Xcode asset
# README. None of it is Taro's, so none of it belongs in his graph.
SKIP_DIR_NAMES = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "vendor", "Pods", "Carthage", "DerivedData", ".dart_tool", ".flutter-plugins",
    "build", "dist", "out", "target", ".next", ".nuxt", ".svelte-kit", ".expo",
    "site-packages", ".cocoapods", ".gradle", ".terraform", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".turbo", ".cache",
    "coverage", ".idea", ".vscode", ".cursor", ".nvm", ".oh-my-zsh",
    "bin", "obj", "packages",
    # WordPress ships hundreds of READMEs that belong to WordPress.
    "wp-includes", "wp-content", "wp-admin",
    ".github", ".gitlab",
}

# Boilerplate that is present in every repo and says nothing about the work.
SKIP_FILE_STEMS = {
    "license", "licence", "code_of_conduct", "pull_request_template",
    "issue_template", "security", "changelog",
}

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB
TEXT_SUFFIXES = {".md", ".txt"}
PDF_SUFFIXES = {".pdf"}
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")

# [label](target "optional title") - the ordinary markdown link.
MDLINK_RE = re.compile(r'''\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+"[^"]*")?\s*\)''')

# Filenames that make a good hub for a repository, best first.
REPO_ANCHOR_NAMES = ["readme.md", "claude.md", "agents.md", "index.md", "readme.txt"]

# Stems too generic to be a title on their own - use the folder instead.
GENERIC_STEMS = {"readme", "index", "claude", "agents", "codex", "doc", "docs", "notes"}

# ...and folders too generic to identify a note, when we have to fall back
# to a folder name. "docs" tells you nothing; the repo above it does.
GENERIC_DIRS = {
    "docs", "doc", "src", "app", "lib", "notes", "rules", "skills",
    "commands", ".claude", ".agents",
}

MIN_TITLE_LEN = 3


@dataclass
class Note:
    id: str                # relative path, used as the stable node id
    title: str
    type: str               # top-level folder name this note lives under
    path: Path
    content: str
    excerpt: str
    links_out: list[str] = field(default_factory=list)  # [[titles]] this note links to
    links_md: list[str] = field(default_factory=list)   # relative paths it links to
    repo: str = ""          # repository directory this note belongs to
    repo_name: str = ""     # its bare name, shown in the inspector
    is_repo: bool = False   # bucket came from a real .git root
    degree: int = 0


@dataclass
class VaultIndex:
    notes: dict[str, Note] = field(default_factory=dict)      # id -> Note
    title_to_id: dict[str, str] = field(default_factory=dict)  # lowercase title -> id
    edges: list[tuple[str, str]] = field(default_factory=list)  # (id, id)
    edge_kinds: dict = field(default_factory=dict)    # (id, id) -> wikilink|link|repo
    type_labels: dict = field(default_factory=dict)   # type -> human name
    skipped: list[str] = field(default_factory=list)  # human-readable skip reasons

    def counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for note in self.notes.values():
            counts[note.type] = counts.get(note.type, 0) + 1
        return counts

    def top_hubs(self, n: int = 10) -> list[Note]:
        return sorted(self.notes.values(), key=lambda x: x.degree, reverse=True)[:n]

    def neighbors(self, note_id: str) -> list[str]:
        out = []
        for a, b in self.edges:
            if a == note_id:
                out.append(b)
            elif b == note_id:
                out.append(a)
        return out

    def shortest_path(self, start_id: str, end_id: str) -> list[str] | None:
        """BFS shortest path between two note ids, or None if disconnected."""
        if start_id not in self.notes or end_id not in self.notes:
            return None
        if start_id == end_id:
            return [start_id]
        from collections import deque

        visited = {start_id}
        queue = deque([[start_id]])
        while queue:
            path = queue.popleft()
            for nxt in self.neighbors(path[-1]):
                if nxt == end_id:
                    return path + [nxt]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])
        return None

    def to_graph_json(self) -> dict:
        return {
            "nodes": [
                {
                    "id": note.id,
                    "title": note.title,
                    "type": note.type,
                    "degree": note.degree,
                    "excerpt": note.excerpt,
                    "repo": note.repo_name,
                }
                for note in self.notes.values()
            ],
            "types": {
                t: {
                    "label": self.type_labels.get(t, t),
                    "count": n,
                    # Whether this bucket is a repository or just a folder -
                    # the UI names the panel after whichever it is, so a
                    # plain notes vault isn't labelled "Projects".
                    "repo": any(note.is_repo for note in self.notes.values()
                                if note.type == t),
                }
                for t, n in self.counts_by_type().items()
            },
            "edges": [
                {"source": a, "target": b,
                 "kind": self.edge_kinds.get((a, b), "link")}
                for a, b in self.edges
            ],
        }


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _read_pdf_best_effort(path: Path) -> str | None:
    """
    Extremely small, stdlib-only, best-effort PDF text extraction.
    Pulls parenthesis-delimited strings out of BT/ET text blocks. Works
    for simple, uncompressed PDFs; silently returns whatever it can for
    anything more complex rather than pulling in a PDF library.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("latin-1", errors="ignore")
    except Exception:
        return None
    chunks = re.findall(r"\((?:[^()\\]|\\.)*\)", text)
    cleaned = " ".join(c[1:-1] for c in chunks)
    cleaned = cleaned.replace("\\(", "(").replace("\\)", ")")
    return cleaned.strip() or None


def _humanize(stem: str) -> str:
    return " ".join(w for w in re.split(r"[_\-\s]+", stem) if w).strip() or stem


def _qualifying_dir(path: Path) -> str:
    """Nearest ancestor folder whose name actually identifies something."""
    for parent in path.parents:
        if parent.name and parent.name.lower() not in GENERIC_DIRS:
            return parent.name
    return path.parent.name


def _derive_title(content: str, path: Path) -> str:
    """
    First H1 wins. Failing that, the filename - except when the filename is
    something every repo has ("README", "doc"), where the folder it sits in
    is the only part that actually identifies it.
    """
    if path.stem.lower() in INSTRUCTION_STEMS:
        return f"{_humanize(path.parent.name)} · {path.stem.upper()}"

    # PDFs never get the H1 treatment. Their text comes out of a
    # stdlib-only, best-effort extractor that decodes as latin-1, so a
    # Japanese PDF yields mojibake - and a "# " landing inside that garbage
    # would become the node's label. The filename is the honest answer.
    if path.suffix.lower() not in PDF_SUFFIXES:
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("# "):
                title = line[2:].strip()
                # "# or", "# db": real headings, useless as names.
                if len(title) >= MIN_TITLE_LEN:
                    return title
                break

    stem = path.stem
    title = _humanize(path.parent.name) if stem.lower() in GENERIC_STEMS else _humanize(stem)
    if len(title) < MIN_TITLE_LEN:
        title = f"{_humanize(_qualifying_dir(path))} · {title}"
    return title


# CLAUDE.md / AGENTS.md open with a section heading ("# Who this is for"),
# not a title, so their H1 is actively misleading. Name them by folder.
INSTRUCTION_STEMS = {"claude", "agents", "codex"}


def _iter_files(folders: Iterable[Path]) -> Iterable[Path]:
    """
    Walks with os.walk rather than rglob so that skipped directories are
    PRUNED instead of visited-then-discarded. Over real repositories that is
    the difference between 25 seconds and a fraction of one: rglob descends
    into every node_modules and vendor tree in full before the filter ever
    sees a path.

    Pruning by name also gets the root case right for free - os.walk only
    ever hands us directories below the configured root, so pointing the
    vault at a folder called "build" or "dist" still reads it.
    """
    for folder in folders:
        folder = Path(folder)
        if not folder.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
            for name in sorted(filenames):
                if Path(name).stem.lower() in SKIP_FILE_STEMS:
                    continue
                yield Path(dirpath) / name


_repo_root_cache: dict[str, Path | None] = {}


def _find_repo_root(path: Path, stop_at: Path) -> Path | None:
    """
    Nearest ancestor containing .git, at or below `stop_at`.

    The naive "first folder under the configured root" rule breaks on any
    folder that groups several checkouts - ~/Documents/Works/next-surprise
    holds eight separate repositories, and treating them as one lumps every
    doc onto a single mega-hub. .git is where the real boundary is.
    """
    current = path if path.is_dir() else path.parent
    chain: list[Path] = []
    while True:
        key = str(current)
        if key in _repo_root_cache:
            found = _repo_root_cache[key]
            break
        chain.append(current)
        if (current / ".git").exists():
            found = current
            break
        if current == stop_at or current.parent == current:
            found = None
            break
        current = current.parent
    for d in chain:
        _repo_root_cache[str(d)] = found
    return found


def _resolve(path: Path) -> str:
    """Absolute, symlink-free path as a string, for identity comparisons."""
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def _resolve_link(source: Path, target: str) -> str | None:
    """
    Turns a markdown link target into an absolute path, or None if it isn't
    one - URLs, mail links and bare "#anchor" jumps all land here.
    """
    target = target.strip()
    if not target or target.startswith(("#", "http://", "https://", "mailto:", "tel:", "//")):
        return None
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    target = unquote(target)
    try:
        return _resolve(source.parent / target)
    except (OSError, ValueError):
        return None


def _add_edge(index: VaultIndex, a: str | None, b: str | None, kind: str) -> None:
    """Undirected, deduped. The first kind to claim a pair keeps it."""
    if not a or not b or a == b:
        return
    edge = tuple(sorted((a, b)))
    if edge in index.edge_kinds:
        return
    index.edge_kinds[edge] = kind
    index.edges.append(edge)


# Two repositories sitting side by side under one folder are one piece of
# work - the five groundy-* checkouts are a single product, the three under
# ishizaka/ are a single client. Beyond this many, side-by-side stops
# meaning anything and the clique would just be a hairball.
MAX_GROUP_SIZE = 6

# If the author's own links already reach this share of the notes, the
# structure is theirs and machine-derived membership edges would only bury
# it. A notes vault clears this easily; a folder of repo docs never does.
AUTHORED_LINK_COVERAGE = 0.6


def _repo_groups(index: VaultIndex) -> dict[str, tuple[str, list[str]]]:
    """
    Maps each repository's anchor note id to (repo path, other note ids).
    The anchor is the repo's README (or CLAUDE.md, or AGENTS.md); failing
    all of those, the note nearest the repository root.
    """
    by_repo: dict[str, list[Note]] = {}
    for note in index.notes.values():
        if note.repo:
            by_repo.setdefault(note.repo, []).append(note)

    groups: dict[str, list[str]] = {}
    for repo, notes in by_repo.items():
        if len(notes) < 2:
            continue
        anchor = None
        for name in REPO_ANCHOR_NAMES:
            anchor = next(
                (n for n in notes
                 if n.path.name.lower() == name and str(n.path.parent) == repo),
                None,
            )
            if anchor:
                break
        if anchor is None:
            anchor = min(notes, key=lambda n: (len(n.path.parts), str(n.path)))
        groups[anchor.id] = (repo, [n.id for n in notes if n.id != anchor.id])
    return groups


def _type_labels(index: VaultIndex) -> dict:
    """
    A folder slug is not a project name. "seo_ai_system" is what the
    directory is called; "SEO Analytics System - MVP" is what Taro calls it,
    and it's already written at the top of that repo's README - so read it
    from there rather than title-casing the slug into "Seo Ai System".

    Only repositories get this: a plain notes folder's name ("Research") is
    already the right label, and its top note's title is not.
    """
    labels: dict[str, str] = {}
    best_rank: dict[str, int] = {}
    for note in index.notes.values():
        if not note.is_repo or str(note.path.parent) != note.repo:
            continue
        try:
            rank = REPO_ANCHOR_NAMES.index(note.path.name.lower())
        except ValueError:
            continue
        if rank < best_rank.get(note.type, len(REPO_ANCHOR_NAMES)):
            best_rank[note.type] = rank
            labels[note.type] = note.title
    return labels


def _link_coverage(index: VaultIndex) -> float:
    """Share of notes that the author's own links already touch."""
    if not index.notes:
        return 1.0
    linked = set()
    for a, b in index.edges:
        linked.add(a)
        linked.add(b)
    return len(linked) / len(index.notes)


def _add_membership_edges(index: VaultIndex) -> None:
    """
    Pass 4 - repository membership: every doc joins its repo's README, so a
    codebase reads as a cluster instead of unconnected dust.

    Pass 5 - sibling repositories: without it the graph is a row of
    unconnected stars; with it, one product's checkouts read as one
    constellation. The fact behind that edge is only "these two repos sit in
    the same folder", which is why it's the faintest kind on screen.

    Both run after the real links, so a pair already joined by one keeps
    the stronger kind.
    """
    groups = _repo_groups(index)
    for anchor_id, (_repo, member_ids) in groups.items():
        for member_id in member_ids:
            _add_edge(index, member_id, anchor_id, "repo")

    by_parent: dict[str, list[str]] = {}
    for anchor_id, (repo, _members) in groups.items():
        by_parent.setdefault(str(Path(repo).parent), []).append(anchor_id)
    for siblings in by_parent.values():
        if not 2 <= len(siblings) <= MAX_GROUP_SIZE:
            continue
        for i, a in enumerate(siblings):
            for b in siblings[i + 1:]:
                _add_edge(index, a, b, "group")


def build_index(folders: Iterable[Path]) -> VaultIndex:
    index = VaultIndex()
    folders = [Path(f) for f in folders]
    path_to_id: dict[str, str] = {}

    # Pass 1: read every eligible file into a Note.
    for path in _iter_files(folders):
        suffix = path.suffix.lower()
        if suffix not in TEXT_SUFFIXES and suffix not in PDF_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            index.skipped.append(f"{path} (over 2MB)")
            continue

        if suffix in TEXT_SUFFIXES:
            content = _read_text_file(path)
        else:
            content = _read_pdf_best_effort(path)
        if content is None:
            index.skipped.append(f"{path} (unreadable)")
            continue

        # type = the folder directly under the configured root this file
        # lives in. For a notes vault that's "Projects" / "Research"; for a
        # folder of repositories it's the repository name, which makes the
        # Filter panel a per-repo switch.
        rel_root = next((f for f in folders if f in path.parents), None)
        if rel_root is not None:
            rel = path.relative_to(rel_root)
            # The bucket a note is filed under - what the Filter panel lists
            # and what colour it gets - is its repository, because that is
            # the unit of work a person actually thinks in. Configure a repo
            # root directly and every file in it shares one bucket, however
            # deep it sits. With no repository anywhere above it, fall back
            # to the top folder under the root, which is how a plain notes
            # vault ("Research", "Journal") wants to be grouped.
            # Namespaced by root: two configured roots each holding a
            # README.md would otherwise both claim the id "README.md" and
            # the second would silently overwrite the first.
            note_id = str(Path(rel_root.name) / rel)
            if note_id in index.notes:
                note_id = _resolve(path)
            git_root = _find_repo_root(path, rel_root)
            if git_root is not None:
                repo_dir = git_root
                note_type = git_root.name
                is_repo = True
            else:
                repo_dir = (rel_root / rel.parts[0]) if len(rel.parts) > 1 else rel_root
                note_type = rel.parts[0] if len(rel.parts) > 1 else rel_root.name
                is_repo = False
        else:
            note_id = str(path)
            repo_dir = path.parent
            note_type = path.parent.name
            is_repo = False

        title = _derive_title(content, path)
        excerpt = " ".join(content.split())[:220]

        note = Note(
            id=note_id,
            title=title,
            type=note_type.lower(),
            path=path,
            content=content,
            excerpt=excerpt,
            repo=str(repo_dir),
            repo_name=repo_dir.name,
            is_repo=is_repo,
        )
        note.links_out = WIKILINK_RE.findall(content)
        note.links_md = MDLINK_RE.findall(content)
        index.notes[note_id] = note
        # First writer wins: repo docs repeat titles ("Testing", "Database")
        # across repositories, and silently reassigning would make a
        # [[wikilink]] resolve to whichever file happened to be walked last.
        index.title_to_id.setdefault(title.lower(), note_id)
        path_to_id[_resolve(path)] = note_id

    # Pass 2: [[wikilinks]], resolved by title now that every title is known.
    for note in index.notes.values():
        for linked_title in note.links_out:
            target_id = index.title_to_id.get(linked_title.strip().lower())
            _add_edge(index, note.id, target_id, "wikilink")

    # Pass 3: ordinary markdown links, resolved by relative path. This is the
    # only kind of link real repository docs actually contain.
    for note in index.notes.values():
        for target in note.links_md:
            target_id = path_to_id.get(_resolve_link(note.path, target))
            _add_edge(index, note.id, target_id, "link")

    index.type_labels = _type_labels(index)

    # Passes 4 and 5 are the machine-derived edges, and they only run when
    # the author hasn't already linked their own notes together.
    if _link_coverage(index) < AUTHORED_LINK_COVERAGE:
        _add_membership_edges(index)

    # Degree = number of edges touching each note.

    for a, b in index.edges:
        index.notes[a].degree += 1
        index.notes[b].degree += 1

    return index


def print_report(index: VaultIndex) -> None:
    total = len(index.notes)
    print(f"Indexed {total} notes, {len(index.edges)} links")
    kinds: dict[str, int] = {}
    for kind in index.edge_kinds.values():
        kinds[kind] = kinds.get(kind, 0) + 1
    if kinds:
        print("  " + ", ".join(f"{k}: {n}" for k, n in sorted(kinds.items())))
    print()
    print("By type:")
    for note_type, count in sorted(index.counts_by_type().items(), key=lambda kv: -kv[1]):
        print(f"  {note_type:<12} {count}")
    print()
    print("Top hubs:")
    for note in index.top_hubs(10):
        print(f"  {note.degree:>2} links  {note.title}  ({note.type})")
    if index.skipped:
        print()
        print(f"Skipped {len(index.skipped)} file(s):")
        for s in index.skipped[:20]:
            print(f"  {s}")


if __name__ == "__main__":
    # Standalone run: index whatever data.py is configured to read and
    # print the report.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.data import VAULT_FOLDERS

    print(f"Indexing: {', '.join(str(f) for f in VAULT_FOLDERS)}")
    print()
    idx = build_index(VAULT_FOLDERS)
    print_report(idx)
