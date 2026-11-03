import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

from sqlalchemy import select, text

from .errors import AppError
from .media import inspect_file
from .models import (
    Asset,
    AssetRelation,
    Blob,
    Execution,
    ProjectAsset,
    ProjectAssetTag,
    ProjectTag,
    Task,
    record_event,
    row_dict,
    uid,
)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


class Storage:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.root = settings.data_dir
        self.file_lock = asyncio.Lock()

    def check_space(self):
        free = shutil.disk_usage(self.root).free
        if free < self.settings.minimum_free_bytes:
            raise AppError(
                "disk_space",
                "素材磁盘空间低于保留限制。请释放空间后继续；已有作品不会被删除。",
                details={"free_bytes": free},
            )

    def resolve(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise AppError("invalid_path", "文件路径不在应用素材目录内。")
        return path

    async def blob_path(self, asset_id, session=None):
        if session is None:
            async with self.db.session() as s:
                return await self.blob_path(asset_id, s)
        asset = await session.get(Asset, asset_id)
        if not asset:
            raise AppError("asset_not_found", "素材不存在。", 404)
        blob = await session.get(Blob, asset.blob_id)
        path = self.resolve(blob.relative_path)
        if blob.presence != "available" or not path.is_file():
            raise AppError("asset_missing", "这份素材的原文件已缺失，无法作为输入。", 409, {"asset_id": asset_id})
        return asset, blob, path

    async def stage_upload(self, upload):
        self.check_space()
        target = self.root / "staging" / (uid() + ".upload")
        total = 0
        try:
            with target.open("wb") as file:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.settings.max_import_file_bytes:
                        raise AppError("file_size", "单文件超过导入上限。")
                    self.check_space()
                    await asyncio.to_thread(file.write, chunk)
                await asyncio.to_thread(file.flush)
                await asyncio.to_thread(os.fsync, file.fileno())
            if total == 0:
                raise AppError("empty_file", "文件内容为空。")
            return target
        except Exception:
            target.unlink(missing_ok=True)
            raise

    async def ingest(self, staged, name, project_id, **options):
        staged = Path(staged)
        sha = await asyncio.to_thread(file_hash, staged)
        receipt = self.root / "staging" / (uid() + ".receipt.json")
        data = {
            "sha256": sha,
            "staged": staged.relative_to(self.root).as_posix(),
            "name": name,
            "project_id": project_id,
            "options": options,
        }
        await asyncio.to_thread(atomic_json, receipt, data)
        result = await self._ingest(staged, name, project_id, **options)
        receipt.unlink(missing_ok=True)
        return result

    async def recover_receipts(self):
        import logging

        for receipt in (self.root / "staging").glob("*.receipt.json"):
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
                staged = self.resolve(data["staged"])
                if not staged.is_file():
                    staged = self.resolve(f"blobs/{data['sha256'][:2]}/{data['sha256']}")
                if not staged.is_file() or await asyncio.to_thread(file_hash, staged) != data["sha256"]:
                    continue
                await self._ingest(staged, data["name"], data["project_id"], **data["options"])
                receipt.unlink(missing_ok=True)
            except Exception:
                logging.getLogger("aiv.storage").exception("保存日志恢复失败，保留日志等待处理")

    async def _ingest(
        self,
        staged,
        name,
        project_id,
        *,
        origin="imported",
        role="imported",
        description="",
        source_info=None,
        execution_id=None,
        candidate_index=None,
        inputs=None,
        artifact_id=None,
    ):
        asset_id = uid()
        preview = self.root / "previews" / (asset_id + ".png")
        sha = await asyncio.to_thread(file_hash, staged)
        metadata = await asyncio.to_thread(inspect_file, staged, name, preview)
        size = staged.stat().st_size
        target = self.root / "blobs" / sha[:2] / sha
        async with self.file_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.stat().st_size != size or await asyncio.to_thread(file_hash, target) != sha:
                    raise AppError("blob_integrity", "已有内容文件校验失败；未覆盖文件。", 409)
                if staged.resolve() != target.resolve():
                    staged.unlink(missing_ok=True)
            else:
                await asyncio.to_thread(os.replace, staged, target)
        async with self.db.transaction() as session:
            blob = await session.get(Blob, sha)
            if not blob:
                blob = Blob(
                    sha256=sha,
                    byte_size=size,
                    relative_path=target.relative_to(self.root).as_posix(),
                    mime_type=metadata["mime_type"],
                    extension=Path(name).suffix.lower()[:12] or ".bin",
                )
                session.add(blob)
                await session.flush()
            elif blob.presence != "available":
                blob.presence = "available"
            existing = None
            if artifact_id:
                from .models import Artifact

                artifact = await session.get(Artifact, artifact_id)
                if artifact.asset_id:
                    existing = await session.get(Asset, artifact.asset_id)
                    if existing and existing.blob_id != sha:
                        raise AppError("artifact_integrity", "补存内容与原素材不同，已保留证据并停止覆盖。", 409)
                    if existing:
                        existing.presence = "available"
                        artifact.state, artifact.error, artifact.staging_path = "saved", None, None
            if origin == "imported":
                existing = await session.scalar(
                    select(Asset)
                    .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                    .where(
                        ProjectAsset.project_id == project_id,
                        ProjectAsset.removed_at.is_(None),
                        Asset.blob_id == sha,
                        Asset.origin == "imported",
                    )
                )
            if existing:
                if metadata["preview_path"] and (
                    not existing.preview_path or not self.resolve("previews/" + existing.preview_path).is_file()
                ):
                    existing.preview_path = metadata["preview_path"]
                else:
                    preview.unlink(missing_ok=True)
                existing.presence = "available"
                record_event(session, "asset.saved", project_id, existing.id, asset_id=existing.id)
                return existing.id
            asset = Asset(
                id=asset_id,
                blob_id=sha,
                name=Path(name).name,
                origin=origin,
                role=role,
                description=description,
                source_info=source_info or {},
                execution_id=execution_id,
                candidate_index=candidate_index,
                **{k: v for k, v in metadata.items() if k != "mime_type"},
            )
            session.add(asset)
            await session.flush()
            session.add(
                ProjectAsset(
                    project_id=project_id, asset_id=asset_id, usage="output" if origin == "generated" else "material"
                )
            )
            for item in inputs or []:
                session.add(
                    AssetRelation(
                        from_asset_id=item["asset_id"],
                        to_asset_id=asset_id,
                        execution_id=execution_id,
                        slot=item["slot"],
                        relation_type="derived",
                    )
                )
            if artifact_id:
                from .models import Artifact

                artifact = await session.get(Artifact, artifact_id)
                artifact.asset_id = asset_id
                artifact.saved_blob = sha
                artifact.state = "saved"
                artifact.staging_path = None
                artifact.error = None
            await self.index(session, project_id, asset)
            record_event(session, "asset.saved", project_id, asset_id, asset_id=asset_id)
        return asset_id

    async def tags_for(self, session, project_id, asset_ids):
        """Return normalized tags grouped by asset id for one project."""
        ids = list(dict.fromkeys(asset_ids))
        if not ids:
            return {}
        rows = (
            await session.execute(
                select(ProjectAssetTag.asset_id, ProjectTag.id, ProjectTag.name, ProjectTag.color)
                .join(ProjectTag, (ProjectTag.id == ProjectAssetTag.tag_id) & (ProjectTag.project_id == ProjectAssetTag.project_id))
                .where(ProjectAssetTag.project_id == project_id, ProjectAssetTag.asset_id.in_(ids))
                .order_by(ProjectTag.position, ProjectTag.name, ProjectTag.id)
            )
        ).all()
        result = {}
        for asset_id, tag_id, name, color in rows:
            result.setdefault(asset_id, []).append({"id": tag_id, "name": name, "color": color})
        return result

    async def index(self, session, project_id, asset, note="", tags=None):
        from .models import Project

        project = await session.get(Project, project_id)
        membership = await session.get(ProjectAsset, (project_id, asset.id))
        tag_names = list(
            await session.scalars(
                select(ProjectTag.name)
                .join(
                    ProjectAssetTag,
                    (ProjectAssetTag.tag_id == ProjectTag.id) & (ProjectAssetTag.project_id == ProjectTag.project_id),
                )
                .where(ProjectAssetTag.project_id == project_id, ProjectAssetTag.asset_id == asset.id)
                .order_by(ProjectTag.position, ProjectTag.name)
            )
        )
        if not tag_names and membership and membership.tags:
            # Fallback for records that predate the normalized tag backfill.
            tag_names = list(membership.tags)
        value = " ".join(
            [
                asset.name,
                (membership.display_name or "") if membership else "",
                asset.description,
                project.name,
                membership.note if membership else note,
                " ".join(tag_names),
                asset.source_info.get("tool_name", ""),
            ]
        ).casefold()
        key = f"{project_id}:{asset.id}"
        await session.execute(text("DELETE FROM asset_search WHERE entry_id=:key"), {"key": key})
        await session.execute(
            text("INSERT INTO asset_search(entry_id,project_id,asset_id,body) VALUES (:key,:project,:asset,:body)"),
            {"key": key, "project": project_id, "asset": asset.id, "body": value},
        )

    async def asset_view(self, session, asset, membership=None, tags=None):
        blob = await session.get(Blob, asset.blob_id)
        result = row_dict(asset)
        result["original_name"] = asset.name
        result["conversation_id"] = (
            await session.scalar(
                select(Task.conversation_id)
                .join(Execution, Execution.task_id == Task.id)
                .where(Execution.id == asset.execution_id)
            )
            if asset.execution_id
            else None
        )
        path = self.resolve(blob.relative_path)
        available = blob.presence == "available" and path.is_file()
        result.update(
            presence="available" if available else "purged" if blob.presence == "purged" else "missing",
            byte_size=blob.byte_size,
            mime_type=blob.mime_type,
            content_url=f"/api/v1/assets/{asset.id}/content",
            preview_url=f"/api/v1/assets/{asset.id}/preview"
            if asset.preview_path and blob.presence != "purged" and asset.presence != "purged"
            else None,
        )
        if membership:
            project = row_dict(membership)
            if tags is None:
                tags = await self.tags_for(session, membership.project_id, [asset.id])
            project["tags"] = tags.get(asset.id, [])
            project["curated"] = bool(membership.curated_at)
            result["project"] = project
            if membership.display_name:
                result["name"] = membership.display_name
        return result

    async def reconcile(self):
        # Persist missing-file facts; do not manufacture a successful recovery.
        async with self.db.transaction() as session:
            assets = (await session.scalars(select(Asset))).all()
            for asset in assets:
                blob = await session.get(Blob, asset.blob_id)
                present = blob.presence == "available" and self.resolve(blob.relative_path).is_file()
                state = "available" if present else "purged" if blob.presence == "purged" else "missing"
                if asset.presence != state:
                    asset.presence = state
                    record_event(
                        session,
                        "asset.missing" if not present else "asset.saved",
                        entity_id=asset.id,
                        asset_id=asset.id,
                    )
