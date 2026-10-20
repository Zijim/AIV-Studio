"""Desktop entry point: reuse or start the local service, then open the browser."""

import argparse
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger("aiv.launcher")


def read_health(opener, url):
    try:
        with opener.open(url + "/api/v1/health", timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def check_identity(health, data_dir):
    if (
        health.get("status") != "ok"
        or Path(health.get("workspace_root", "")).resolve() != ROOT
        or Path(health.get("data_dir", "")).resolve() != data_dir
    ):
        raise RuntimeError("工作台端口已被其他实例占用，请先关闭另一个工作台。")


@contextlib.contextmanager
def startup_lock(data_dir):
    import msvcrt

    with (data_dir / "launcher.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 70
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("另一个工作台正在启动，请稍后再试。") from None
                time.sleep(0.25)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_service(settings):
    data_dir = settings.data_dir
    url = f"http://127.0.0.1:{settings.port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with startup_lock(data_dir):
        health = read_health(opener, url)
        if health:
            check_identity(health, data_dir)
            logger.info("Reusing workbench PID %s", health.get("process_id"))
            return url
        if settings.host not in ("127.0.0.1", "localhost"):
            raise RuntimeError("工作台配置需要使用本机地址127.0.0.1或localhost。")
        python = ROOT / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            raise RuntimeError("工作台Python环境不存在，请先运行scripts/setup.ps1。")
        logs = data_dir / "logs"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        stderr = logs / f"server-{stamp}.stderr.log"
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend"),
            "AIV_DATA_DIR": str(data_dir),
        }
        with (
            (logs / f"server-{stamp}.stdout.log").open("ab", buffering=0) as out,
            stderr.open("ab", buffering=0) as err,
        ):
            process = subprocess.Popen(
                [str(python), "-m", "app.launch"],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        logger.info("Started launcher PID %s; stderr %s", process.pid, stderr)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            health = read_health(opener, url)
            if health:
                check_identity(health, data_dir)
                logger.info("Workbench ready, PID %s", health.get("process_id"))
                return url
            if process.poll() is not None:
                raise RuntimeError(f"工作台服务启动失败。错误日志：\n{stderr}")
            time.sleep(0.4)
        raise RuntimeError(f"工作台启动超过60秒，请稍后重试。错误日志：\n{stderr}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-directory", type=Path)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start or reuse the service without opening a browser",
    )
    args = parser.parse_args()
    if args.data_directory:
        os.environ["AIV_DATA_DIR"] = str(args.data_directory.resolve())
    sys.path.insert(0, str(ROOT / "backend"))
    from app.config import load_settings

    settings = load_settings()
    logging.basicConfig(
        filename=settings.data_dir / "logs" / "launcher.log",
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    url = ensure_service(settings)
    if not args.no_browser:
        os.startfile(url)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("Unable to open workbench")
        if sys.stderr is not None:
            print(str(exc), file=sys.stderr)
        else:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, str(exc), "映作工作台 · 启动失败", 0x10
            )
        sys.exit(1)
