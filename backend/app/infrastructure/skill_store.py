"""读取随应用分发的独立技能包。

技能是「拷出去就能单独运行」的目录：应用只负责展示、预览和打包下载，
不会替用户安装到某个 Agent 的目录里。这里刻意只暴露文本源码与说明：

- `config.json` 里是用户真实的 API Key / Cookie 路径，永远不进列表和压缩包；
- `vendor/`、`output/`、`models/`、`__pycache__/` 也不是技能本体，
  体积大且因机器而异，打包下载时一并排除。

开发版从仓库的 `skills/` 读取；打包版优先从 PyInstaller 的资源目录读取，
也兼容把 `skills/` 放在 exe 同级的部署方式。
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

# 只允许这些文本后缀进入列表与压缩包。
TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".cfg",
    ".ini",
    ".sh",
    ".ps1",
}

# 这些目录属于运行环境或用户产物，不是技能源码。
EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "vendor",
    "output",
    "models",
    "node_modules",
}

# 真实配置里有明文密钥，只随技能目录存在，不对外展示、不打包。
# Cookie 文件也按名字挡掉：技能说明里让用户自行放在目录外，万一放进来了也不能分发。
EXCLUDED_FILES = {
    "config.json",
    "cookies.txt",
    "cookie.txt",
    "secrets.json",
    "credentials.json",
}

# 单个文件预览上限：技能里的说明和源码都很小，超过这个量就不再回传全文。
PREVIEW_LIMIT = 240_000

KIND_BY_SUFFIX = {
    ".md": "markdown",
    ".py": "python",
    ".json": "json",
}


@dataclass(frozen=True, slots=True)
class SkillFile:
    path: str
    size: int
    kind: str


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    title: str
    description: str
    version: str | None
    root: Path
    files: tuple[SkillFile, ...]
    updated_at: float

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def download_name(self) -> str:
        return f"{self.id}.zip"


def _skill_roots() -> list[Path]:
    """按优先级列出可能的技能根目录，重复路径只留一份。"""

    roots: list[Path] = []
    if settings.skills_dir is not None:
        roots.append(Path(settings.skills_dir).expanduser())
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        roots.append(base / "skills")
        roots.append(Path(sys.executable).parent / "skills")
    # skill_store.py 位于 backend/app/infrastructure/，仓库根在上三级。
    roots.append(Path(__file__).resolve().parents[3] / "skills")

    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _frontmatter(text: str) -> dict[str, str]:
    """读取 SKILL.md 顶部的简单 `key: value` 头，不引入 YAML 依赖。"""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        data[key.strip()] = value
    return data


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def _first_paragraph(text: str) -> str | None:
    """frontmatter 缺失时，用正文第一段非标题文字兜底。"""

    in_frontmatter = False
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if index == 0 and stripped == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if stripped == "---":
                in_frontmatter = False
            continue
        if not stripped or stripped.startswith(("#", "```", "|", ">", "-", "*")):
            continue
        return stripped
    return None


def _iter_files(root: Path):
    # SKILL.md 排第一：它是用户点开技能后最该先看的文件。
    def order(path: Path) -> tuple[int, str]:
        return (0 if path.name == "SKILL.md" else 1, path.relative_to(root).as_posix().lower())

    for path in sorted(root.rglob("*"), key=order):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        if path.name in EXCLUDED_FILES or path.name.startswith("."):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield relative.as_posix(), path


def _load_skill(root: Path) -> Skill | None:
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    meta = _frontmatter(text)
    files: list[SkillFile] = []
    latest = 0.0
    for relative, path in _iter_files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        latest = max(latest, stat.st_mtime)
        files.append(
            SkillFile(
                path=relative,
                size=stat.st_size,
                kind=KIND_BY_SUFFIX.get(path.suffix.lower(), "text"),
            )
        )
    if not files:
        return None

    return Skill(
        id=root.name,
        title=_first_heading(text) or meta.get("name") or root.name,
        description=meta.get("description") or _first_paragraph(text) or "",
        version=meta.get("version") or None,
        root=root,
        files=tuple(files),
        updated_at=latest,
    )


def _children(root: Path) -> list[Path]:
    # 允许 VTW_SKILLS_DIR 直接指向某一个技能目录。
    if (root / "SKILL.md").is_file():
        return [root]
    try:
        return sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name)
    except OSError:
        return []


def list_skills() -> list[Skill]:
    """按根目录优先级合并技能；同 id 只显示优先级最高的那一份。"""

    found: dict[str, Skill] = {}
    for root in _skill_roots():
        for child in _children(root):
            if child.name in found:
                continue
            skill = _load_skill(child)
            if skill is not None:
                found[skill.id] = skill
    return sorted(found.values(), key=lambda item: item.title)


def get_skill(skill_id: str) -> Skill | None:
    return next((skill for skill in list_skills() if skill.id == skill_id), None)


def _safe_relative(skill: Skill, relative: str) -> str | None:
    """把前端传来的路径收敛到该技能的允许文件集合内，挡掉 `..` 穿越。"""

    candidate = Path(relative.replace("\\", "/"))
    if candidate.is_absolute():
        return None
    try:
        resolved = (skill.root / candidate).resolve()
        resolved.relative_to(skill.root)
    except (OSError, ValueError):
        return None
    normalized = resolved.relative_to(skill.root).as_posix()
    return normalized if any(item.path == normalized for item in skill.files) else None


def read_skill_file(skill: Skill, relative: str) -> tuple[str, str, bool] | None:
    """读取一个允许预览的文本文件，返回 (相对路径, 内容, 是否截断)。"""

    normalized = _safe_relative(skill, relative)
    if normalized is None:
        return None
    try:
        content = (skill.root / normalized).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    truncated = len(content) > PREVIEW_LIMIT
    if truncated:
        content = content[:PREVIEW_LIMIT]
    return normalized, content, truncated


def build_skill_zip(skill: Skill) -> bytes:
    """在内存里打 ZIP：只含允许文件，顶层目录用技能 id，解压即得标准技能目录。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in skill.files:
            source = skill.root / item.path
            try:
                archive.write(source, arcname=f"{skill.id}/{item.path}")
            except OSError:
                # 单个文件读不到时不阻断整包下载；列表里它仍然可能存在，
                # 但技能的核心文件（SKILL.md / scripts）通常不会缺失。
                continue
    return buffer.getvalue()
