"""独立技能包：列表、文件预览与 ZIP 下载。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.infrastructure.skill_store import (
    Skill,
    build_skill_zip,
    get_skill,
    list_skills,
    read_skill_file,
)
from app.schemas import SkillFileContentRead, SkillFileRead, SkillListRead, SkillRead

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _skill_or_404(skill_id: str) -> Skill:
    skill = get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"没有找到技能「{skill_id}」")
    return skill


def _serialize(skill: Skill) -> SkillRead:
    return SkillRead(
        id=skill.id,
        title=skill.title,
        description=skill.description,
        version=skill.version,
        files=[
            SkillFileRead(path=item.path, size=item.size, kind=item.kind)
            for item in skill.files
        ],
        total_bytes=skill.total_bytes,
        updated_at=skill.updated_at or None,
        download_url=f"/api/skills/{skill.id}/download",
    )


@router.get("", response_model=SkillListRead)
def list_skill_packages() -> SkillListRead:
    return SkillListRead(items=[_serialize(skill) for skill in list_skills()])


@router.get("/{skill_id}", response_model=SkillRead)
def skill_detail(skill_id: str) -> SkillRead:
    return _serialize(_skill_or_404(skill_id))


@router.get("/{skill_id}/file", response_model=SkillFileContentRead)
def skill_file(
    skill_id: str,
    path: str = Query(min_length=1, max_length=500, description="技能目录内的相对路径"),
) -> SkillFileContentRead:
    skill = _skill_or_404(skill_id)
    result = read_skill_file(skill, path)
    if result is None:
        raise HTTPException(status_code=404, detail="这个文件不在技能包内，或无法预览")
    normalized, content, truncated = result
    return SkillFileContentRead(path=normalized, content=content, truncated=truncated)


@router.get("/{skill_id}/download")
def download_skill(skill_id: str) -> Response:
    skill = _skill_or_404(skill_id)
    payload = build_skill_zip(skill)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            # 文件名只用 ASCII 的 id：中文标题放进 filename* 在不同浏览器里表现不一致，
            # 而技能目录本来就以 id 命名，用户解压后正好得到 video-transcript/。
            "Content-Disposition": f'attachment; filename="{skill.download_name}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )
