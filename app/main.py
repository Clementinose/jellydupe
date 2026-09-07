"""JellyDupe — hitta och rensa dubbletter i ett Jellyfin-bibliotek."""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .jellyfin import Jellyfin, JellyfinError

CONFIG_DIR = Path(os.environ.get("JELLYDUPE_CONFIG", "/config"))
CONFIG_FILE = CONFIG_DIR / "config.json"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="JellyDupe")

_lock = threading.Lock()
_state: Dict = {
    "status": "idle",          # idle | scanning | done | error
    "message": "",
    "error": None,
    "scannedAt": None,
    "server": None,
    "groups": {"movie": [], "episode": []},
}


# ---------------------------------------------------------------- config

def load_config() -> Dict:
    env_url = os.environ.get("JELLYFIN_URL")
    env_key = os.environ.get("JELLYFIN_API_KEY")
    if env_url and env_key:
        return {"url": env_url.rstrip("/"), "apiKey": env_key, "fromEnv": True}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            data["fromEnv"] = False
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"url": "", "apiKey": "", "fromEnv": False}


def save_config(url: str, api_key: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"url": url.rstrip("/"), "apiKey": api_key}, indent=2))


def client() -> Jellyfin:
    cfg = load_config()
    if not cfg.get("url") or not cfg.get("apiKey"):
        raise HTTPException(400, "Jellyfin är inte anslutet ännu.")
    return Jellyfin(cfg["url"], cfg["apiKey"])


# ---------------------------------------------------------------- scan

def _set(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _run_scan() -> None:
    try:
        jf = client()
        info = jf.ping()
        _set(status="scanning", message="Ansluten, hämtar användare …", server=info, error=None)

        user_id = jf.admin_user_id()

        _set(message="Läser filmer …")
        movies = jf.scan_movies(user_id, progress=lambda m: _set(message=m))

        _set(message="Läser avsnitt …")
        episodes = jf.scan_episodes(user_id, progress=lambda m: _set(message=m))

        _set(
            status="done",
            message="",
            groups={"movie": movies, "episode": episodes},
            scannedAt=datetime.now(timezone.utc).isoformat(),
        )
    except (JellyfinError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        _set(status="error", error=detail, message="")
    except Exception as exc:  # noqa: BLE001
        _set(status="error", error=f"Oväntat fel: {exc}", message="")


# ---------------------------------------------------------------- api

class ConfigIn(BaseModel):
    url: str
    apiKey: str


class DeleteIn(BaseModel):
    ids: List[str]
    dryRun: bool = False


@app.get("/api/config")
def get_config():
    cfg = load_config()
    return {
        "url": cfg.get("url", ""),
        "connected": bool(cfg.get("url") and cfg.get("apiKey")),
        "fromEnv": cfg.get("fromEnv", False),
    }


@app.post("/api/config")
def set_config(body: ConfigIn):
    url = body.url.strip().rstrip("/")
    key = body.apiKey.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "Adressen måste börja med http:// eller https://")
    try:
        info = Jellyfin(url, key).ping()
    except JellyfinError as exc:
        raise HTTPException(400, str(exc)) from exc
    save_config(url, key)
    return {"ok": True, "server": info}


@app.get("/api/status")
def status():
    with _lock:
        return {
            "status": _state["status"],
            "message": _state["message"],
            "error": _state["error"],
            "scannedAt": _state["scannedAt"],
            "server": _state["server"],
            "counts": {k: len(v) for k, v in _state["groups"].items()},
        }


@app.post("/api/scan")
def scan():
    with _lock:
        if _state["status"] == "scanning":
            return {"ok": True, "already": True}
        _state.update(status="scanning", message="Startar …", error=None)
    threading.Thread(target=_run_scan, daemon=True).start()
    return {"ok": True}


@app.get("/api/groups")
def groups(kind: str = "movie", q: Optional[str] = None):
    if kind not in ("movie", "episode"):
        raise HTTPException(400, "kind måste vara movie eller episode")
    with _lock:
        data = list(_state["groups"][kind])
    if q:
        needle = q.lower()
        data = [
            g
            for g in data
            if needle in (g.get("title") or "").lower()
            or needle in (g.get("series") or "").lower()
        ]
    return {
        "groups": data,
        "totals": {
            "groups": len(data),
            "files": sum(g["count"] for g in data),
            "wasted": sum(g["wastedSize"] for g in data),
        },
    }


@app.post("/api/delete")
def delete(body: DeleteIn):
    jf = client()
    results = []
    for item_id in body.ids:
        if body.dryRun:
            results.append({"id": item_id, "ok": True, "dryRun": True})
            continue
        try:
            jf.delete(item_id)
            results.append({"id": item_id, "ok": True})
        except JellyfinError as exc:
            results.append({"id": item_id, "ok": False, "error": str(exc)})

    ok_ids = {r["id"] for r in results if r["ok"] and not body.dryRun}
    if ok_ids:
        with _lock:
            for kind, glist in _state["groups"].items():
                kept = []
                for g in glist:
                    versions = [v for v in g["versions"] if v["sourceId"] not in ok_ids]
                    if len(versions) < 2:
                        continue
                    best = max(versions, key=lambda v: (v["height"] or 0, v["bitrate"] or 0))
                    for v in versions:
                        v["best"] = v is best
                    g["versions"] = versions
                    g["count"] = len(versions)
                    g["totalSize"] = sum(v["size"] for v in versions)
                    g["wastedSize"] = g["totalSize"] - best["size"]
                    kept.append(g)
                _state["groups"][kind] = kept

    failed = [r for r in results if not r["ok"]]
    return {"results": results, "deleted": len(results) - len(failed), "failed": len(failed)}


@app.get("/api/image/{item_id}")
def image(item_id: str):
    """Proxar omslag så att webbläsaren inte behöver nå Jellyfin direkt."""
    cfg = load_config()
    if not cfg.get("url"):
        raise HTTPException(404, "Inget omslag")
    url = f"{cfg['url']}/Items/{item_id}/Images/Primary"
    try:
        r = requests.get(
            url,
            params={"maxHeight": 260, "quality": 85},
            headers={"X-Emby-Token": cfg.get("apiKey", "")},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, str(exc)) from exc
    if r.status_code != 200:
        raise HTTPException(404, "Inget omslag")
    return Response(
        content=r.content,
        media_type=r.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------- static

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
