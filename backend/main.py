from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator


class CredentialPayload(BaseModel):
    username: str | None = None
    password: str | None = None
    credentials_json: str | None = None


APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.getenv("ZOTIFY_CONFIG_DIR", APP_ROOT / "config"))
CONFIG_FILE = CONFIG_DIR / "config.json"
FRONTEND_FILE = APP_ROOT / "frontend" / "index.html"

DEFAULT_CONFIG: dict[str, Any] = {
    "download_format": "ogg",
    "download_quality": "auto",
    "output": "{artist}/{album}/{track_number} - {song_name}.{ext}",
    "root_path": str(APP_ROOT / "music"),
    "credentials_location": str(CONFIG_DIR / "credentials.json"),
    "download_real_time": False,
    "download_lyrics": False,
    "skip_existing": False,
    "skip_previously_downloaded": False,
    "split_album_discs": False,
    "md_allgenres": False,
    "override_auto_wait": False,
    "transcode_bitrate": None,
    "retry_attempts": 1,
    "bulk_wait_time": 1,
    "chunk_size": 20000,
}


class ConfigModel(BaseModel):
    download_format: str = "ogg"
    download_quality: str = "auto"
    output: str = DEFAULT_CONFIG["output"]
    root_path: str = DEFAULT_CONFIG["root_path"]
    credentials_location: str = DEFAULT_CONFIG["credentials_location"]
    download_real_time: bool = False
    download_lyrics: bool = False
    skip_existing: bool = False
    skip_previously_downloaded: bool = False
    split_album_discs: bool = False
    md_allgenres: bool = False
    override_auto_wait: bool = False
    transcode_bitrate: int | None = Field(default=None, ge=1, le=10000)
    retry_attempts: int = Field(default=1, ge=0, le=100)
    bulk_wait_time: int = Field(default=1, ge=0, le=86400)
    chunk_size: int = Field(default=20000, ge=1, le=1000000)

    @field_validator("download_format")
    @classmethod
    def valid_format(cls, value: str) -> str:
        if value not in {"ogg", "mp3", "flac", "aac", "m4a", "opus", "vorbis"}:
            raise ValueError("Unsupported download format")
        return value

    @field_validator("download_quality")
    @classmethod
    def valid_quality(cls, value: str) -> str:
        if value not in {"auto", "normal", "high", "very_high"}:
            raise ValueError("Unsupported download quality")
        return value


class QueueRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    config: ConfigModel | None = None


@dataclass
class QueueItem:
    id: str
    url: str
    status: str = "queued"
    error: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


class AppState:
    def __init__(self) -> None:
        self.queue: list[QueueItem] = []
        self.queue_lock = asyncio.Lock()
        self.queue_event = asyncio.Event()
        self.clients: set[WebSocket] = set()
        self.clients_lock = asyncio.Lock()
        self.worker_task: asyncio.Task[None] | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.config = self.load_config()

    @staticmethod
    def load_config() -> dict[str, Any]:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            return DEFAULT_CONFIG.copy()
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **loaded}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Unable to read {CONFIG_FILE}: {exc}", file=sys.stderr)
            return DEFAULT_CONFIG.copy()

    def save_config(self, config: dict[str, Any]) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
        temporary.replace(CONFIG_FILE)
        self.config = config

    async def broadcast(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message)
        async with self.clients_lock:
            clients = list(self.clients)
        disconnected: list[WebSocket] = []
        for client in clients:
            try:
                await client.send_text(payload)
            except (WebSocketDisconnect, RuntimeError, OSError):
                disconnected.append(client)
        if disconnected:
            async with self.clients_lock:
                self.clients.difference_update(disconnected)

    async def add_item(self, item: QueueItem) -> None:
        async with self.queue_lock:
            self.queue.append(item)
        self.queue_event.set()
        await self.broadcast({"type": "queue", "items": queue_as_dict(self.queue)})

    async def worker(self) -> None:
        while True:
            await self.queue_event.wait()
            while True:
                async with self.queue_lock:
                    item = next((entry for entry in self.queue if entry.status == "queued"), None)
                if item is None:
                    self.queue_event.clear()
                    break
                await self.run_item(item)

    async def run_item(self, item: QueueItem) -> None:
        item.status = "running"
        await self.broadcast({"type": "queue", "items": queue_as_dict(self.queue)})
        await self.broadcast({"type": "log", "stream": "system", "text": f"Starting {item.url}\n"})
        try:
            command = build_command(item.url, item.config)
            await self.broadcast({"type": "log", "stream": "system", "text": f"$ {' '.join(command)}\n"})
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=APP_ROOT,
                start_new_session=(os.name != "nt"),
            )
            await asyncio.gather(
                stream_output(self.process.stdout, "stdout", self.broadcast),
                stream_output(self.process.stderr, "stderr", self.broadcast),
            )
            return_code = await self.process.wait()
            if return_code == 0:
                item.status = "completed"
                await self.broadcast({"type": "log", "stream": "system", "text": "Completed successfully.\n"})
            else:
                item.status = "failed"
                item.error = f"Zotify exited with code {return_code}"
                await self.broadcast({"type": "log", "stream": "stderr", "text": f"{item.error}\n"})
        except (OSError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                if self.process and self.process.returncode is None:
                    self.process.terminate()
                raise
            item.status = "failed"
            item.error = str(exc)
            await self.broadcast({"type": "log", "stream": "stderr", "text": f"Failed to start Zotify: {exc}\n"})
        finally:
            self.process = None
            await self.broadcast({"type": "queue", "items": queue_as_dict(self.queue)})


def queue_as_dict(items: list[QueueItem]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]


async def stream_output(
    stream: asyncio.StreamReader | None,
    name: str,
    broadcast: Any,
) -> None:
    if stream is None:
        return
    while line := await stream.readline():
        await broadcast({"type": "log", "stream": name, "text": line.decode(errors="replace")})


def ensure_zotify_installed() -> str | None:
    executable = shutil.which("zotify")
    if executable:
        return executable
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "git+https://github.com/zotify-dev/zotify.git"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError:
        return None
    return shutil.which("zotify")


def build_command(url: str, config: dict[str, Any]) -> list[str]:
    executable = ensure_zotify_installed() or shutil.which("zotify")
    command = [executable or sys.executable]
    if not executable:
        command.extend(["-m", "zotify"])
    command.append(url)
    options = {
        "--download-format": config["download_format"],
        "--download-quality": config["download_quality"],
        "--output": config["output"],
        "--root-path": config["root_path"],
        "--credentials-location": config["credentials_location"],
        "--retry-attempts": str(config["retry_attempts"]),
        "--bulk-wait-time": str(config["bulk_wait_time"]),
        "--chunk-size": str(config["chunk_size"]),
    }
    if config.get("transcode_bitrate") is not None:
        options["--transcode-bitrate"] = str(config["transcode_bitrate"])
    for flag, value in options.items():
        command.extend([flag, value])
    for key in (
        "download_real_time",
        "download_lyrics",
        "skip_existing",
        "skip_previously_downloaded",
        "split_album_discs",
        "md_allgenres",
        "override_auto_wait",
    ):
        if config.get(key):
            command.extend([f"--{key.replace('_', '-')}", "true"])
    return command


state = AppState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.worker_task = asyncio.create_task(state.worker())
    yield
    if state.worker_task:
        state.worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await state.worker_task
    if state.process and state.process.returncode is None:
        state.process.terminate()
        with suppress(ProcessLookupError):
            await state.process.wait()


app = FastAPI(title="Zotify Local Studio", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_FILE)


@app.get("/api/config", response_model=ConfigModel)
async def get_config() -> dict[str, Any]:
    return state.config


@app.put("/api/config", response_model=ConfigModel)
async def update_config(config: ConfigModel) -> dict[str, Any]:
    state.save_config(config.model_dump())
    return state.config


@app.get("/api/queue")
async def get_queue() -> dict[str, Any]:
    async with state.queue_lock:
        return {"items": queue_as_dict(state.queue)}


@app.delete("/api/queue/completed")
async def clear_completed() -> dict[str, Any]:
    async with state.queue_lock:
        state.queue = [item for item in state.queue if item.status not in {"completed", "failed"}]
        items = queue_as_dict(state.queue)
    await state.broadcast({"type": "queue", "items": items})
    return {"items": items}


@app.post("/api/queue", status_code=202)
async def enqueue(request: QueueRequest) -> dict[str, Any]:
    config = (request.config or ConfigModel(**state.config)).model_dump()
    state.save_config(config)
    item = QueueItem(id=uuid.uuid4().hex, url=request.url.strip(), config=config)
    await state.add_item(item)
    return asdict(item)


@app.post("/api/credentials")
async def upload_credentials(request: Request, file: UploadFile | None = File(default=None)) -> dict[str, str]:
    destination = CONFIG_DIR / "credentials.json"

    if file is not None:
        if file.filename != "credentials.json":
            raise HTTPException(status_code=400, detail="Upload a file named credentials.json")
        content = await file.read()
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="credentials.json must contain valid JSON") from exc
        destination.write_bytes(content)
        state.config["credentials_location"] = str(destination)
        state.save_config(state.config)
        return {"path": str(destination)}

    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise HTTPException(status_code=400, detail="Provide either credentials JSON or a credentials.json upload")

    payload = CredentialPayload(**await request.json())
    raw_text = payload.credentials_json.strip() if payload.credentials_json else ""
    if raw_text:
        try:
            json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="credentials JSON paste must contain valid JSON") from exc
        content = raw_text.encode("utf-8")
    elif payload.username or payload.password:
        if not payload.username or not payload.password:
            raise HTTPException(status_code=400, detail="Username and password are both required")
        content = json.dumps({"username": payload.username.strip(), "password": payload.password.strip()}).encode("utf-8")
    else:
        raise HTTPException(status_code=400, detail="Provide username/password or credentials JSON")

    destination.write_bytes(content)
    state.config["credentials_location"] = str(destination)
    state.save_config(state.config)
    return {"path": str(destination)}


@app.websocket("/ws")
async def websocket_logs(websocket: WebSocket) -> None:
    await websocket.accept()
    async with state.clients_lock:
        state.clients.add(websocket)
    try:
        await websocket.send_json({"type": "queue", "items": queue_as_dict(state.queue)})
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        async with state.clients_lock:
            state.clients.discard(websocket)
