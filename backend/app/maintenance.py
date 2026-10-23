import asyncio
import json
import shutil
import sqlite3
import zipfile
from contextlib import closing

from sqlalchemy import select

from .errors import AppError
from .models import Asset, Blob, Execution, Maintenance, Plan, Project, ProjectAsset, Task, now, row_dict, uid
from .plans import ACTIVE_TASKS, digest
from .storage import atomic_json, file_hash


class MaintenanceService:
    def __init__(self, db, settings, storage):
        self.db, self.settings, self.storage = db, settings, storage
        self.lock = asyncio.Lock()

    async def ensure_idle(self, s):
        from .models import AgentTurn

        active = await s.scalar(select(Task).where(Task.state.in_(ACTIVE_TASKS)).limit(1))
        planning = await s.scalar(select(AgentTurn).where(AgentTurn.state == "planning").limit(1))
        if active or planning:
            raise AppError("maintenance_busy", "请等待当前任务和助手方案准备结束，再进行备份或永久清理。", 409)

    async def candidates(self, s):
        rows = (
            await s.execute(
                select(Asset, Blob).join(Blob, Asset.blob_id == Blob.sha256).where(Blob.presence != "purged")
            )
        ).all()
        protected = set(
            (
                await s.scalars(
                    select(Asset.blob_id)
                    .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
                    .join(Project, Project.id == ProjectAsset.project_id)
                    .where(ProjectAsset.removed_at.is_(None), Project.removed_at.is_(None))
                )
            ).all()
        )
        plans = (await s.scalars(select(Plan).where(Plan.state == "draft"))).all()
        active = (await s.scalars(select(Execution).join(Task).where(Task.state.in_(ACTIVE_TASKS)))).all()
        pinned_ids = {i["asset_id"] for p in plans for i in p.inputs} | {
            i["asset_id"] for e in active for i in e.inputs
        }
        for a, b in rows:
            if a.id in pinned_ids:
                protected.add(b.sha256)
        groups = {}
        for a, b in rows:
            if b.sha256 not in protected:
                groups.setdefault(b.sha256, {"blob_id": b.sha256, "bytes": b.byte_size, "assets": []})["assets"].append(
                    {"id": a.id, "name": a.name}
                )
        return sorted(groups.values(), key=lambda b: b["blob_id"])

    async def preview_gc(self):
        async with self.db.transaction() as s:
            candidates = await self.candidates(s)
            data = {"files": candidates, "byte_size": sum(c["bytes"] for c in candidates), "hash": digest(candidates)}
            job = Maintenance(id=uid(), kind="gc", state="preview", data=data)
            s.add(job)
            await s.flush()
            return row_dict(job)

    async def gc(self, job_id, expected_hash):
        async with self.lock:
            async with self.storage.file_lock:
                async with self.db.transaction() as s:
                    await self.ensure_idle(s)
                    job = await s.get(Maintenance, job_id)
                    if not job or job.kind != "gc" or job.state != "preview" or job.data["hash"] != expected_hash:
                        raise AppError("gc_preview", "清理预览不存在或已经处理，请重新预览。", 409)
                    candidates = await self.candidates(s)
                    if digest(candidates) != expected_hash:
                        raise AppError("gc_changed", "文件引用已变化，请重新预览清理影响。", 409)
                    job.state = "committing"
                async with self.db.transaction() as s:
                    await self.ensure_idle(s)
                    job = await s.get(Maintenance, job_id)
                    candidates = await self.candidates(s)
                    if digest(candidates) != expected_hash:
                        raise AppError("gc_changed", "文件引用已变化，请重新预览清理影响。", 409)
                    for item in candidates:
                        blob = await s.get(Blob, item["blob_id"])
                        self.storage.resolve(blob.relative_path).unlink(missing_ok=True)
                        blob.presence = "purged"
                        for affected in item["assets"]:
                            asset = await s.get(Asset, affected["id"])
                            asset.presence = "purged"
                            if asset.preview_path:
                                self.storage.resolve("previews/" + asset.preview_path).unlink(missing_ok=True)
                    job.state = "completed"
                    return {"removed_files": len(candidates), "released_bytes": sum(c["bytes"] for c in candidates)}

    async def recover(self):
        async with self.db.transaction() as s:
            jobs = (
                await s.scalars(select(Maintenance).where(Maintenance.kind == "gc", Maintenance.state == "committing"))
            ).all()
            for job in jobs:
                for item in job.data["files"]:
                    blob = await s.get(Blob, item["blob_id"])
                    if blob and not self.storage.resolve(blob.relative_path).is_file():
                        blob.presence = "purged"
                        for asset in (await s.scalars(select(Asset).where(Asset.blob_id == blob.sha256))).all():
                            asset.presence = "purged"
                job.state = "interrupted"

    async def backup(self):
        async with self.lock:
            async with self.storage.file_lock:
                async with self.db.transaction() as s:
                    await self.ensure_idle(s)
                    self.storage.check_space()
                    job_id = uid()
                    target = self.settings.data_dir / "backups" / (job_id + ".zip")
                    data = await asyncio.to_thread(self._backup, target)
                    job = Maintenance(id=job_id, kind="backup", state="completed", data=data)
                    s.add(job)
                    await s.flush()
                    return row_dict(job)

    def _backup(self, target):
        root = self.settings.data_dir
        snapshot = root / "staging" / (uid() + ".sqlite3")
        manifest = {"format_version": 1, "created_at": now(), "files": [], "missing_files": []}
        try:
            with closing(sqlite3.connect(root / "app.sqlite3")) as src, closing(sqlite3.connect(snapshot)) as dest:
                src.backup(dest)
            files = [("app.sqlite3", snapshot)]
            for folder in ("blobs", "previews", "registry", "executions", "prompt_guides"):
                files.extend(
                    (p.relative_to(root).as_posix(), p)
                    for p in (root / folder).rglob("*")
                    if p.is_file() and not p.name.endswith(".partial")
                )
            required = sum(path.stat().st_size for name, path in files)
            if shutil.disk_usage(root).free < required + self.settings.minimum_free_bytes:
                raise AppError("disk_space", "磁盘空间不足以创建完整备份，请先释放空间。")
            with closing(sqlite3.connect(snapshot)) as db:
                for sha, rel in db.execute("SELECT sha256,relative_path FROM blobs WHERE presence='available'"):
                    if not (root / rel).is_file():
                        manifest["missing_files"].append({"blob_id": sha, "path": rel})
            with zipfile.ZipFile(
                target.with_suffix(".partial"), "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as archive:
                for name, path in files:
                    manifest["files"].append({"path": name, "bytes": path.stat().st_size, "sha256": file_hash(path)})
                    archive.write(path, name)
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            target.with_suffix(".partial").replace(target)
            return {
                "filename": target.name,
                "bytes": target.stat().st_size,
                "sha256": file_hash(target),
                "file_count": len(files),
                "missing_files": manifest["missing_files"],
            }
        finally:
            snapshot.unlink(missing_ok=True)

    async def restore(self, staged):
        async with self.lock:
            destination = self.settings.data_dir.parent / ("restore-" + uid())
            try:
                result = await asyncio.to_thread(self._restore, staged, destination)
            except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise AppError("backup_invalid", "备份文件格式或清单无效，未启用恢复目录。") from exc
            staged.unlink(missing_ok=True)
            return result

    def _restore(self, staged, destination):
        with zipfile.ZipFile(staged) as archive:
            if archive.getinfo("manifest.json").file_size > 8 * 1024**2:
                raise AppError("backup_manifest", "备份清单过大。")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format_version") != 1:
                raise AppError("backup_version", "备份格式不受支持。")
            self.storage.check_space()
            total = sum(entry["bytes"] for entry in manifest["files"])
            if total + self.settings.minimum_free_bytes > shutil.disk_usage(self.settings.data_dir).free:
                raise AppError("disk_space", "磁盘空间不足以恢复此备份。")
            destination.mkdir(parents=True)
            names = set()
            for entry in manifest["files"]:
                name = entry["path"]
                path = (destination / name).resolve()
                if name in names or not path.is_relative_to(destination) or name.startswith(("/", "\\")) or ":" in name:
                    raise AppError("backup_path", "备份包含无效路径。")
                if name != "app.sqlite3" and name.split("/")[0] not in (
                    "blobs",
                    "previews",
                    "registry",
                    "executions",
                    "prompt_guides",
                ):
                    raise AppError("backup_path", "备份包含未授权目录。")
                names.add(name)
                info = archive.getinfo(name)
                if info.file_size != entry["bytes"] or info.file_size > self.settings.max_import_file_bytes:
                    raise AppError("backup_size", "备份文件大小与清单不一致。")
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, path.open("wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                if file_hash(path) != entry["sha256"]:
                    raise AppError("backup_integrity", "备份校验失败，未启用恢复目录。")
        with closing(sqlite3.connect(destination / "app.sqlite3")) as db:
            if (
                db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or db.execute("PRAGMA foreign_key_check").fetchone()
            ):
                raise AppError("backup_database", "恢复数据库未通过完整性检查。")
        atomic_json(destination / "restore-verification.json", {"verified_at": now(), "source_manifest": manifest})
        return {
            "directory": str(destination),
            "verified": True,
            "message": "备份已恢复到独立目录。关闭应用后，将config.toml的app.data_dir设置为该目录再启动。",
        }
