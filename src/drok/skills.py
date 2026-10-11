"""Skill library, Hermes-style progressive disclosure, shared by every agent.

Level 0: name + description of every skill sits in each agent's prompt.
Level 1: skill_view(name) loads SKILL.md.
Level 2: skill_view(name, file) loads a supporting file (references/, scripts/, templates/, assets/).

Skills live in /workspace/.skills so their scripts run directly on the computer. Agents create and patch them as they
learn; usage is tracked, and the curator archives agent-made skills nobody has used in a long time (never deletes).
"""

from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

import yaml

from .app import parse_frontmatter, slugify
from .db import now

SUBDIRS = ("references", "scripts", "templates", "assets")
ARCHIVE_AFTER_DAYS = 60
BODY_WARN = 12_000


class Skills:
    def __init__(self, root: Path, seeds: Path, db=None, legacy_root: Path | None = None):
        self.root = root
        self.db = db
        self.archive_dir = root / ".archive"
        if not root.exists():
            if legacy_root and legacy_root.exists():
                shutil.move(str(legacy_root), str(root))
            else:
                root.mkdir(parents=True)
                for d in seeds.iterdir() if seeds.exists() else []:
                    if (d / "SKILL.md").exists():
                        shutil.copytree(d, root / d.name)

    # ---------- reading ----------
    def _usage(self) -> dict[str, dict]:
        return {r["slug"]: r for r in self.db.all("SELECT * FROM skill_usage")} if self.db else {}

    def list(self, archived: bool = False) -> list[dict]:
        base = self.archive_dir if archived else self.root
        usage = self._usage()
        out = []
        for f in sorted(base.glob("*/SKILL.md")):
            meta, _ = parse_frontmatter(f.read_text())
            u = usage.get(f.parent.name, {})
            out.append({"slug": f.parent.name, "name": meta.get("name", f.parent.name),
                        "description": meta.get("description", ""), "draft": bool(meta.get("draft")),
                        "created_by": meta.get("created_by", "owner"), "uses": u.get("uses", 0),
                        "last_used": u.get("last_used"), "pinned": bool(u.get("pinned")),
                        "patches": u.get("patches", 0), "files": self._files(f.parent)})
        return out

    def _dir(self, slug: str) -> Path:
        return self.root / slugify(slug)

    @staticmethod
    def _files(d: Path) -> list[str]:
        return sorted(str(p.relative_to(d)) for sub in SUBDIRS for p in (d / sub).rglob("*") if p.is_file())

    def read(self, slug: str) -> dict | None:
        f = self._dir(slug) / "SKILL.md"
        if not f.exists():
            return None
        raw = f.read_text()
        meta, body = parse_frontmatter(raw)
        return {"slug": f.parent.name, **meta, "body": body, "raw": raw, "files": self._files(f.parent)}

    def view(self, slug: str, file: str = "") -> str:
        skill = self.read(slug)
        if not skill:
            close = difflib.get_close_matches(slugify(slug), [s["slug"] for s in self.list()], n=3)
            return f"No skill '{slug}'." + (f" Did you mean: {', '.join(close)}?" if close else "")
        if file:
            path = self._safe_file(slug, file)
            if not path.exists():
                return f"No file '{file}' in skill {skill['slug']}. Files: {', '.join(skill['files']) or 'none'}"
            return path.read_text(errors="replace")[:40_000]
        self._touch(skill["slug"], "uses")
        files = (f"\n\n---\nSupporting files (skill_view with file=…; scripts run from "
                 f"/workspace/.skills/{skill['slug']}/): {', '.join(skill['files'])}") if skill["files"] else ""
        return f"# Skill: {skill['slug']}\n\n{skill['body']}{files}"

    # ---------- writing ----------
    def save(self, name: str, description: str, body: str, draft: bool = False, created_by: str = "owner") -> str:
        slug = slugify(name)
        meta = {"name": slug, "description": " ".join(description.split())}
        if draft:
            meta["draft"] = True
        meta["created_by"] = created_by
        self.write_raw(slug, f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)}---\n\n{body.strip()}\n")
        return slug

    def write_raw(self, slug: str, raw: str) -> None:
        d = self._dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(raw)
        if self.db:
            self.db.run("INSERT OR IGNORE INTO skill_usage (slug, created_at) VALUES (?, ?)", slugify(slug), now())

    def patch(self, slug: str, old: str, new: str) -> str:
        skill = self.read(slug)
        if not skill:
            raise ValueError(f"No skill '{slug}'")
        count = skill["raw"].count(old) if old else 0
        if count != 1:
            raise ValueError(f"old_string must appear exactly once in the skill; found {count} times.")
        self.write_raw(skill["slug"], skill["raw"].replace(old, new))
        self._touch(skill["slug"], "patches")
        return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "before", "after"))[:3000]

    def _safe_file(self, slug: str, rel: str) -> Path:
        d = self._dir(slug)
        path = (d / rel).resolve()
        if not any(path.is_relative_to((d / sub).resolve()) for sub in SUBDIRS):
            raise ValueError(f"Supporting files must live under {', '.join(SUBDIRS)}/")
        return path

    def write_file(self, slug: str, rel: str, content: str) -> None:
        if not self.read(slug):
            raise ValueError(f"No skill '{slug}'")
        path = self._safe_file(slug, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if rel.startswith("scripts/"):
            path.chmod(0o755)

    def remove_file(self, slug: str, rel: str) -> None:
        path = self._safe_file(slug, rel)
        if path.exists():
            path.unlink()

    def delete(self, slug: str) -> None:
        d = self._dir(slug)
        if d.exists():
            shutil.rmtree(d)

    # ---------- lifecycle ----------
    def _touch(self, slug: str, field: str) -> None:
        if not self.db:
            return
        self.db.run("INSERT OR IGNORE INTO skill_usage (slug, created_at) VALUES (?, ?)", slug, now())
        extra = ", last_used = ?" if field == "uses" else ""
        args = [now()] if extra else []
        self.db.run(f"UPDATE skill_usage SET {field} = {field} + 1{extra} WHERE slug = ?", *args, slug)

    def pin(self, slug: str, pinned: bool) -> None:
        self.db.run("INSERT OR IGNORE INTO skill_usage (slug, created_at) VALUES (?, ?)", slugify(slug), now())
        self.db.run("UPDATE skill_usage SET pinned = ? WHERE slug = ?", int(pinned), slugify(slug))

    def archive(self, slug: str) -> None:
        src = self._dir(slug)
        if src.exists():
            self.archive_dir.mkdir(exist_ok=True)
            dest = self.archive_dir / src.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))

    def restore(self, slug: str) -> None:
        src = self.archive_dir / slugify(slug)
        if src.exists() and not self._dir(slug).exists():
            shutil.move(str(src), str(self._dir(slug)))
            self._touch(slugify(slug), "uses")

    def curate(self) -> list[str]:
        """Archive skills that agents wrote but nobody has used for ARCHIVE_AFTER_DAYS. Owner/seed skills and pinned
        skills are never touched; archiving is reversible from the Skills page."""
        cutoff = now() - ARCHIVE_AFTER_DAYS * 86400
        archived = []
        for s in self.list():
            if s["created_by"] in ("owner", "seed") or s["pinned"]:
                continue
            row = self.db.one("SELECT * FROM skill_usage WHERE slug = ?", s["slug"])
            last = (row and (row["last_used"] or row["created_at"])) or now()
            if last < cutoff:
                self.archive(s["slug"])
                archived.append(s["slug"])
        return archived

    @staticmethod
    def lint(raw: str) -> list[str]:
        meta, body = parse_frontmatter(raw)
        warnings = []
        if not meta.get("description"):
            warnings.append("missing description (agents choose skills by it)")
        elif len(meta["description"]) > 160:
            warnings.append("description over 160 chars - make it one crisp line")
        if len(body) > BODY_WARN:
            warnings.append(f"body is {len(body)} chars - move detail into references/ files")
        if len(re.findall(r"^\s*[-*]?\s*\d{4}-\d{2}-\d{2}", body, re.M)) >= 3:
            warnings.append("reads like a dated log - write lessons as general rules (Pitfall: X - do Y)")
        return warnings
