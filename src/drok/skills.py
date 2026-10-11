"""Skill library: one SKILL.md per folder (Agent Skills layout), shared by every agent."""

import shutil
from pathlib import Path

import yaml

from .app import parse_frontmatter, slugify


class Skills:
    def __init__(self, root: Path, seeds: Path):
        self.root = root
        if not root.exists():
            root.mkdir(parents=True)
            if seeds.exists():
                for d in seeds.iterdir():
                    if (d / "SKILL.md").exists():
                        shutil.copytree(d, root / d.name)

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.root.glob("*/SKILL.md")):
            meta, _ = parse_frontmatter(f.read_text())
            out.append({"slug": f.parent.name, "name": meta.get("name", f.parent.name),
                        "description": meta.get("description", ""), "draft": bool(meta.get("draft"))})
        return out

    def read(self, slug: str) -> dict | None:
        f = self.root / slugify(slug) / "SKILL.md"
        if not f.exists():
            return None
        meta, body = parse_frontmatter(f.read_text())
        return {"slug": f.parent.name, **meta, "body": body, "raw": f.read_text()}

    def save(self, name: str, description: str, body: str, draft: bool = False) -> str:
        slug = slugify(name)
        meta = {"name": slug, "description": description.strip()}
        if draft:
            meta["draft"] = True
        self.write_raw(slug, f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)}---\n\n{body.strip()}\n")
        return slug

    def write_raw(self, slug: str, raw: str) -> None:
        d = self.root / slugify(slug)
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(raw)

    def delete(self, slug: str) -> None:
        d = self.root / slugify(slug)
        if d.exists():
            shutil.rmtree(d)
