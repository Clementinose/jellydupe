"""JellyDupe — find and clean up duplicates in a Jellyfin library."""

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from .jellyfin import Jellyfin, JellyfinError

CONFIG_DIR = Path(os.environ.get("JELLYDUPE_CONFIG", "/config"))
CONFIG_FILE = CONFIG_DIR / "config.json"
STATS_FILE = CONFIG_DIR / "stats.json"
STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "jellydupe_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

app = FastAPI(title="JellyDupe")

_lock = threading.Lock()
_state: Dict = {
    "status": "idle",
    "message": "",
    "error": None,
    "scannedAt": None,
    "server": None,
    "groups": {"movie": [], "episode": []},
}

DEFAULT_OAUTH = {
    "github": {"enabled": False, "clientId": "", "clientSecret": ""},
    "authentik": {"enabled": False, "issuer": "", "clientId": "", "clientSecret": ""},
}


# ---------------------------------------------------------------- full config store

def _read_raw_config() -> Dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_raw_config(data: Dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def load_full_config() -> Dict:
    data = _read_raw_config()
    oauth = json.loads(json.dumps(DEFAULT_OAUTH))
    stored_oauth = data.get("oauth", {})
    oauth["github"].update(stored_oauth.get("github", {}))
    oauth["authentik"].update(stored_oauth.get("authentik", {}))

    session_secret = data.get("sessionSecret") or secrets.token_hex(32)
    if not data.get("sessionSecret"):
        data["sessionSecret"] = session_secret
        _write_raw_config(data)

    env_internal = os.environ.get("JELLYFIN_URL_INTERNAL") or os.environ.get("JELLYFIN_URL")
    env_external = os.environ.get("JELLYFIN_URL_EXTERNAL")
    env_key = os.environ.get("JELLYFIN_API_KEY")
    from_env = bool(env_internal and env_key)

    return {
        "urlInternal": (env_internal.rstrip("/") if from_env else data.get("urlInternal", "")) or "",
        "urlExternal": ((env_external or "").rstrip("/") if from_env else data.get("urlExternal", "")) or "",
        "apiKey": (env_key if from_env else data.get("apiKey", "")) or "",
        "fromEnv": from_env,
        "sessionSecret": session_secret,
        "oauth": oauth,
    }


def save_full_config(patch: Dict) -> None:
    data = _read_raw_config()
    data.update(patch)
    _write_raw_config(data)


def load_config() -> Dict:
    return load_full_config()


def save_config(url_internal: str, url_external: str, api_key: str) -> None:
    save_full_config({
        "urlInternal": url_internal.rstrip("/"),
        "urlExternal": url_external.rstrip("/") if url_external else "",
        "apiKey": api_key,
    })


def get_oauth_config() -> Dict:
    return load_full_config()["oauth"]


def save_oauth_config(oauth: Dict) -> None:
    save_full_config({"oauth": oauth})


def active_url(cfg: Dict) -> str:
    """Prefer internal URL; caller can override via ?prefer=external."""
    return cfg.get("urlInternal") or cfg.get("urlExternal") or ""


def client(prefer: Optional[str] = None) -> Jellyfin:
    cfg = load_config()
    url = cfg.get("urlExternal") if prefer == "external" else cfg.get("urlInternal")
    url = url or active_url(cfg)
    if not url or not cfg.get("apiKey"):
        raise HTTPException(400, "Jellyfin is not connected yet.")
    return Jellyfin(url, cfg["apiKey"])


# ---------------------------------------------------------------- auth (OAuth2 / OIDC)

_oauth_states: Dict[str, str] = {}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(load_full_config()["sessionSecret"])


def make_session_cookie(user: Dict) -> str:
    return _serializer().dumps(user)


def read_session_cookie(token: Optional[str]) -> Optional[Dict]:
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


def auth_required() -> bool:
    oauth = get_oauth_config()
    return bool(oauth["github"]["enabled"] or oauth["authentik"]["enabled"])


_PUBLIC_PREFIXES = ("/auth/", "/static/", "/api/auth-status")


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path in ("/", "/favicon.ico") or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    if not auth_required():
        return await call_next(request)
    user = read_session_cookie(request.cookies.get(SESSION_COOKIE))
    if user:
        return await call_next(request)
    return JSONResponse({"detail": "Sign-in required."}, status_code=401)


@app.get("/api/auth-status")
def auth_status(request: Request):
    oauth = get_oauth_config()
    user = read_session_cookie(request.cookies.get(SESSION_COOKIE))
    return {
        "required": auth_required(),
        "authenticated": bool(user),
        "user": user,
        "providers": {
            "github": bool(oauth["github"]["enabled"]),
            "authentik": bool(oauth["authentik"]["enabled"]),
        },
    }


@app.post("/auth/logout")
def logout():
    resp = Response(status_code=204)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/auth/login/github")
def login_github(request: Request):
    gh = get_oauth_config()["github"]
    if not gh["enabled"] or not gh["clientId"]:
        raise HTTPException(400, "GitHub sign-in isn't configured.")
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = "github"
    redirect_uri = str(request.url_for("callback_github"))
    params = {"client_id": gh["clientId"], "redirect_uri": redirect_uri, "scope": "read:user", "state": state}
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{urlencode(params)}")


@app.get("/auth/callback/github", name="callback_github")
def callback_github(request: Request, code: str = "", state: str = ""):
    if _oauth_states.pop(state, None) != "github":
        raise HTTPException(400, "That sign-in attempt expired. Please try again.")
    gh = get_oauth_config()["github"]
    redirect_uri = str(request.url_for("callback_github"))
    try:
        token_res = requests.post(
            "https://github.com/login/oauth/access_token",
            data={"client_id": gh["clientId"], "client_secret": gh["clientSecret"],
                  "code": code, "redirect_uri": redirect_uri},
            headers={"Accept": "application/json"}, timeout=20,
        )
        token = token_res.json().get("access_token")
        if not token:
            raise HTTPException(400, "GitHub did not return an access token.")
        user_res = requests.get("https://api.github.com/user",
                                 headers={"Authorization": f"Bearer {token}"}, timeout=20)
        gh_user = user_res.json()
    except requests.RequestException as exc:
        raise HTTPException(502, f"Couldn't reach GitHub: {exc}") from exc

    session = make_session_cookie({
        "provider": "github",
        "name": gh_user.get("name") or gh_user.get("login") or "GitHub user",
        "avatar": gh_user.get("avatar_url"),
    })
    resp = RedirectResponse("/")
    resp.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE)
    return resp


@app.get("/auth/login/authentik")
def login_authentik(request: Request):
    ak = get_oauth_config()["authentik"]
    if not ak["enabled"] or not ak["issuer"] or not ak["clientId"]:
        raise HTTPException(400, "Authentik sign-in isn't configured.")
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = "authentik"
    redirect_uri = str(request.url_for("callback_authentik"))
    params = {"client_id": ak["clientId"], "redirect_uri": redirect_uri,
              "response_type": "code", "scope": "openid profile email", "state": state}
    issuer = ak["issuer"].rstrip("/")
    return RedirectResponse(f"{issuer}/application/o/authorize/?{urlencode(params)}")


@app.get("/auth/callback/authentik", name="callback_authentik")
def callback_authentik(request: Request, code: str = "", state: str = ""):
    if _oauth_states.pop(state, None) != "authentik":
        raise HTTPException(400, "That sign-in attempt expired. Please try again.")
    ak = get_oauth_config()["authentik"]
    issuer = ak["issuer"].rstrip("/")
    redirect_uri = str(request.url_for("callback_authentik"))
    try:
        token_res = requests.post(
            f"{issuer}/application/o/token/",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                  "client_id": ak["clientId"], "client_secret": ak["clientSecret"]},
            timeout=20,
        )
        token = token_res.json().get("access_token")
        if not token:
            raise HTTPException(400, "Authentik did not return an access token.")
        user_res = requests.get(f"{issuer}/application/o/userinfo/",
                                 headers={"Authorization": f"Bearer {token}"}, timeout=20)
        ak_user = user_res.json()
    except requests.RequestException as exc:
        raise HTTPException(502, f"Couldn't reach Authentik: {exc}") from exc

    session = make_session_cookie({
        "provider": "authentik",
        "name": ak_user.get("name") or ak_user.get("preferred_username") or "Authentik user",
        "avatar": None,
    })
    resp = RedirectResponse("/")
    resp.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE)
    return resp


# ---------------------------------------------------------------- oauth settings api

class OAuthConfigIn(BaseModel):
    githubEnabled: bool = False
    githubClientId: str = ""
    githubClientSecret: str = ""
    authentikEnabled: bool = False
    authentikIssuer: str = ""
    authentikClientId: str = ""
    authentikClientSecret: str = ""


@app.get("/api/oauth-config")
def get_oauth_config_api():
    o = get_oauth_config()
    return {
        "githubEnabled": o["github"]["enabled"],
        "githubClientId": o["github"]["clientId"],
        "githubHasSecret": bool(o["github"]["clientSecret"]),
        "authentikEnabled": o["authentik"]["enabled"],
        "authentikIssuer": o["authentik"]["issuer"],
        "authentikClientId": o["authentik"]["clientId"],
        "authentikHasSecret": bool(o["authentik"]["clientSecret"]),
    }


@app.post("/api/oauth-config")
def set_oauth_config_api(body: OAuthConfigIn):
    current = get_oauth_config()
    gh_secret = body.githubClientSecret.strip() or current["github"]["clientSecret"]
    ak_secret = body.authentikClientSecret.strip() or current["authentik"]["clientSecret"]
    save_oauth_config({
        "github": {"enabled": body.githubEnabled, "clientId": body.githubClientId.strip(), "clientSecret": gh_secret},
        "authentik": {"enabled": body.authentikEnabled, "issuer": body.authentikIssuer.strip().rstrip("/"),
                      "clientId": body.authentikClientId.strip(), "clientSecret": ak_secret},
    })
    return {"ok": True}


# ---------------------------------------------------------------- stats (space you have saved)

def load_stats() -> Dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"totalBytesDeleted": 0, "totalFilesDeleted": 0, "history": []}


def record_deletion(freed_bytes: int, files_deleted: int, titles: List[str]) -> Dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    stats = load_stats()
    stats["totalBytesDeleted"] = stats.get("totalBytesDeleted", 0) + freed_bytes
    stats["totalFilesDeleted"] = stats.get("totalFilesDeleted", 0) + files_deleted
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "bytes": freed_bytes,
        "files": files_deleted,
        "titles": titles[:10],
    }
    history = stats.get("history", [])
    history.insert(0, entry)
    stats["history"] = history[:50]
    STATS_FILE.write_text(json.dumps(stats, indent=2))
    return stats


# ---------------------------------------------------------------- recommended logic

def compute_recommended(versions: List[Dict]) -> None:
    """Auto-flag versions that are safe/sensible to delete: mutates in place,
    setting v['recommended'] = True for lower-quality duplicates.
    A version is recommended when it is NOT the best AND at least one of:
      - resolution is meaningfully lower (height ratio < 0.85 of best)
      - it's SDR while a HDR/DV version of equal-or-higher res exists
      - bitrate is less than half of the best version's bitrate at the same-ish resolution
    The single best version is never recommended.
    """
    if len(versions) < 2:
        for v in versions:
            v["recommended"] = False
        return

    best = versions[0]  # already sorted best-first by caller
    best_height = best.get("height") or 0
    best_bitrate = best.get("bitrate") or 1

    for v in versions:
        if v is best:
            v["recommended"] = False
            continue
        height = v.get("height") or 0
        bitrate = v.get("bitrate") or 0
        reasons = []
        if best_height and height and height < best_height * 0.85:
            reasons.append("lower resolution")
        if best.get("videoRange") not in (None, "SDR") and v.get("videoRange", "SDR") == "SDR":
            reasons.append("no HDR")
        if height >= best_height * 0.85 and bitrate and bitrate < best_bitrate * 0.5:
            reasons.append("much lower bitrate")
        v["recommended"] = bool(reasons)
        v["recommendReason"] = ", ".join(reasons) if reasons else None


# ---------------------------------------------------------------- scan

def _set(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _run_scan() -> None:
    try:
        jf = client()
        info = jf.ping()
        _set(status="scanning", message="Connected, fetching users…", server=info, error=None)

        user_id = jf.admin_user_id()

        _set(message="Reading movies…")
        movies = jf.scan_movies(user_id, progress=lambda m: _set(message=m))
        for g in movies:
            compute_recommended(g["versions"])

        _set(message="Reading episodes…")
        episodes = jf.scan_episodes(user_id, progress=lambda m: _set(message=m))
        for g in episodes:
            compute_recommended(g["versions"])

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
        _set(status="error", error=f"Unexpected error: {exc}", message="")


# ---------------------------------------------------------------- api models

class ConfigIn(BaseModel):
    urlInternal: str
    urlExternal: str = ""
    apiKey: str


class DeleteIn(BaseModel):
    ids: List[str]
    dryRun: bool = False


# ---------------------------------------------------------------- config endpoints

@app.get("/api/config")
def get_config():
    cfg = load_config()
    return {
        "urlInternal": cfg.get("urlInternal", ""),
        "urlExternal": cfg.get("urlExternal", ""),
        "connected": bool(active_url(cfg) and cfg.get("apiKey")),
        "fromEnv": cfg.get("fromEnv", False),
    }


@app.post("/api/config")
def set_config(body: ConfigIn):
    url_internal = body.urlInternal.strip().rstrip("/")
    url_external = body.urlExternal.strip().rstrip("/")
    key = body.apiKey.strip()
    if not url_internal and not url_external:
        raise HTTPException(400, "Provide at least one server address.")
    check_url = url_internal or url_external
    if not check_url.startswith("http"):
        raise HTTPException(400, "Address must start with http:// or https://")
    try:
        info = Jellyfin(check_url, key).ping()
    except JellyfinError as exc:
        raise HTTPException(400, str(exc)) from exc
    save_config(url_internal, url_external, key)
    return {"ok": True, "server": info}


# ---------------------------------------------------------------- stats endpoints

@app.get("/api/stats")
def get_stats():
    return load_stats()


# ---------------------------------------------------------------- status / scan

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
        _state.update(status="scanning", message="Starting…", error=None)
    threading.Thread(target=_run_scan, daemon=True).start()
    return {"ok": True}


@app.get("/api/groups")
def groups(kind: str = "movie", q: Optional[str] = None):
    if kind not in ("movie", "episode"):
        raise HTTPException(400, "kind must be movie or episode")
    with _lock:
        data = list(_state["groups"][kind])
    if q:
        needle = q.lower()
        data = [
            g for g in data
            if needle in (g.get("title") or "").lower()
            or needle in (g.get("series") or "").lower()
        ]
    recommended_bytes = sum(
        v["size"] for g in data for v in g["versions"] if v.get("recommended")
    )
    return {
        "groups": data,
        "totals": {
            "groups": len(data),
            "files": sum(g["count"] for g in data),
            "wasted": sum(g["wastedSize"] for g in data),
            "recommendedBytes": recommended_bytes,
        },
    }


@app.post("/api/delete")
def delete(body: DeleteIn):
    jf = client()
    results = []

    with _lock:
        all_versions = {
            v["sourceId"]: v
            for glist in _state["groups"].values()
            for g in glist
            for v in g["versions"]
        }
        titles = {
            v["sourceId"]: g.get("title") or g.get("series")
            for glist in _state["groups"].values()
            for g in glist
            for v in g["versions"]
        }

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
        freed = sum(all_versions[i]["size"] for i in ok_ids if i in all_versions)
        deleted_titles = list({titles[i] for i in ok_ids if i in titles and titles[i]})
        record_deletion(freed, len(ok_ids), deleted_titles)

        with _lock:
            for kind, glist in _state["groups"].items():
                kept = []
                for g in glist:
                    versions = [v for v in g["versions"] if v["sourceId"] not in ok_ids]
                    if len(versions) < 2:
                        continue
                    versions.sort(key=lambda v: (v["height"] or 0, v["bitrate"] or 0, v["size"] or 0), reverse=True)
                    for v in versions:
                        v["best"] = v is versions[0]
                    compute_recommended(versions)
                    g["versions"] = versions
                    g["count"] = len(versions)
                    g["totalSize"] = sum(v["size"] for v in versions)
                    g["wastedSize"] = g["totalSize"] - versions[0]["size"]
                    kept.append(g)
                _state["groups"][kind] = kept

    failed = [r for r in results if not r["ok"]]
    return {"results": results, "deleted": len(results) - len(failed), "failed": len(failed)}


# ---------------------------------------------------------------- image proxy

@app.get("/api/image/{item_id}")
def image(item_id: str):
    cfg = load_config()
    url = active_url(cfg)
    if not url:
        raise HTTPException(404, "No image")
    try:
        r = requests.get(
            f"{url}/Items/{item_id}/Images/Primary",
            params={"maxHeight": 260, "quality": 85},
            headers={"X-Emby-Token": cfg.get("apiKey", "")},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, str(exc)) from exc
    if r.status_code != 200:
        raise HTTPException(404, "No image")
    return Response(content=r.content, media_type=r.headers.get("Content-Type", "image/jpeg"),
                     headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- static

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
