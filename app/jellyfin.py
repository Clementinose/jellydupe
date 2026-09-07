"""Jellyfin client + duplicate-detection logic for JellyDupe."""

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

import requests

PAGE_SIZE = 400
TIMEOUT = 60

MOVIE_FIELDS = "MediaSources,Path,ProviderIds,ProductionYear,DateCreated"
EPISODE_FIELDS = "MediaSources,Path,ProviderIds,SeriesInfo,DateCreated"

# Explicit SxxExx in the filename is unambiguous ground truth — Jellyfin
# sometimes mis-tags episodes (whole batches collapsing to one episode
# number) for unmatched/obscure anime releases, so this always wins.
_SXXEXX_RE = re.compile(r'[Ss](\d{1,3})[Ee](\d{1,4})')
# Loose "trailing episode number" pattern for raw scene releases with no
# season/episode markup at all, e.g. "[Group] Show - 083.mkv".
_EP_NUM_RE = re.compile(r'(?:^|[ ._\-])(?:e|ep|episode)?0*(\d{2,4})(?:v\d+)?(?:[ ._\-]|$)', re.IGNORECASE)


def _filename_sxxexx(path: str) -> "tuple[Optional[int], Optional[int]]":
    base = (path or "").rsplit("/", 1)[-1]
    m = _SXXEXX_RE.search(base)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _filename_episode_number(path: str) -> Optional[int]:
    base = (path or "").rsplit("/", 1)[-1]
    base = re.sub(r'\.\w{2,4}$', '', base)
    matches = _EP_NUM_RE.findall(base)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def resolve_season_episode(path: str, jf_season: Optional[int], jf_number: Optional[int]):
    """Figure out the real (season, episode) for a file, distrusting Jellyfin
    when the filename itself disagrees or fills in a gap. Order of trust:
    1. Explicit SxxExx in the filename — unambiguous, always wins.
    2. A loose trailing episode number in the filename (common in raw scene/anime
       releases with no season markup) combined with Jellyfin's season, if any.
    3. Jellyfin's own parsed fields.
    """
    fs, fn = _filename_sxxexx(path)
    if fs is not None and fn is not None:
        return fs, fn

    loose = _filename_episode_number(path)
    if loose is not None:
        season = jf_season if jf_season is not None else 1
        return season, loose

    return jf_season, jf_number


class JellyfinError(Exception):
    pass


def _norm(text: str) -> str:
    """Normaliserar en titel så att 'The Matrix (1999)' och 'Matrix' matchar."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"^(the|a|an|de|den|det)\s+", "", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _res_label(width: Optional[int], height: Optional[int]) -> str:
    if not height:
        return "Okänd"
    if height >= 2000 or (width or 0) >= 3800:
        return "4K"
    if height >= 1400:
        return "1440p"
    if height >= 900:
        return "1080p"
    if height >= 700:
        return "720p"
    if height >= 500:
        return "576p"
    return f"{height}p"


class Jellyfin:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Emby-Token": self.api_key,
                "Accept": "application/json",
            }
        )

    # ---------- lågnivå ----------

    def _get(self, path: str, **params) -> Any:
        url = f"{self.base_url}{path}"
        try:
            r = self.session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise JellyfinError(f"Når inte {self.base_url}: {exc}") from exc
        if r.status_code == 401:
            raise JellyfinError("Jellyfin svarade 401 — API-nyckeln godtogs inte.")
        if r.status_code >= 400:
            raise JellyfinError(f"Jellyfin svarade {r.status_code} på {path}")
        return r.json()

    def ping(self) -> Dict[str, Any]:
        info = self._get("/System/Info")
        return {
            "serverName": info.get("ServerName"),
            "version": info.get("Version"),
        }

    def admin_user_id(self) -> str:
        users = self._get("/Users")
        for u in users:
            if u.get("Policy", {}).get("IsAdministrator"):
                return u["Id"]
        if users:
            return users[0]["Id"]
        raise JellyfinError("Hittade inga användare på servern.")

    def _items(self, user_id: str, item_type: str, fields: str) -> Iterable[Dict]:
        start = 0
        while True:
            data = self._get(
                "/Items",
                userId=user_id,
                Recursive="true",
                IncludeItemTypes=item_type,
                Fields=fields,
                StartIndex=start,
                Limit=PAGE_SIZE,
                EnableTotalRecordCount="true",
                SortBy="SortName",
            )
            batch = data.get("Items", [])
            total = data.get("TotalRecordCount", len(batch))
            if not batch:
                return
            for item in batch:
                yield item
            start += len(batch)
            if start >= total:
                return

    # ---------- versioner ----------

    @staticmethod
    def _versions(item: Dict) -> List[Dict]:
        """Plattar ut varje MediaSource till en fil med teknisk metadata."""
        out = []
        for src in item.get("MediaSources") or []:
            streams = src.get("MediaStreams") or []
            video = next((s for s in streams if s.get("Type") == "Video"), {})
            audio = next((s for s in streams if s.get("Type") == "Audio"), {})
            subs = [s for s in streams if s.get("Type") == "Subtitle"]

            width = video.get("Width")
            height = video.get("Height")
            size = src.get("Size") or 0
            bitrate = src.get("Bitrate") or video.get("BitRate") or 0
            ticks = src.get("RunTimeTicks") or item.get("RunTimeTicks") or 0

            out.append(
                {
                    "itemId": item.get("Id"),
                    # För sammanslagna versioner är MediaSource.Id själv ett item-id,
                    # och det är det vi raderar.
                    "sourceId": src.get("Id") or item.get("Id"),
                    "path": src.get("Path") or item.get("Path") or "",
                    "container": (src.get("Container") or "").lower(),
                    "size": size,
                    "bitrate": bitrate,
                    "runtime": int(ticks / 10_000_000) if ticks else 0,
                    "added": item.get("DateCreated"),
                    "width": width,
                    "height": height,
                    "resolution": _res_label(width, height),
                    "videoCodec": (video.get("Codec") or "").upper(),
                    "videoRange": video.get("VideoRange") or "SDR",
                    "audioCodec": (audio.get("Codec") or "").upper(),
                    "audioChannels": audio.get("Channels") or 0,
                    "audioLanguage": audio.get("Language") or "",
                    "subtitles": len(subs),
                }
            )
        return out

    # ---------- skanning ----------

    def scan_movies(self, user_id: str, progress=None) -> List[Dict]:
        buckets: Dict[str, Dict] = {}
        seen = 0
        for item in self._items(user_id, "Movie", MOVIE_FIELDS):
            seen += 1
            if progress and seen % 100 == 0:
                progress(f"Läser filmer … {seen}")

            ids = item.get("ProviderIds") or {}
            key = None
            for provider in ("Tmdb", "Imdb", "Tvdb"):
                for k, v in ids.items():
                    if k.lower() == provider.lower() and v:
                        key = f"{provider}:{v}"
                        break
                if key:
                    break
            if not key:
                key = f"name:{_norm(item.get('Name', ''))}:{item.get('ProductionYear') or ''}"

            bucket = buckets.setdefault(
                key,
                {
                    "key": key,
                    "kind": "movie",
                    "title": item.get("Name") or "Okänd titel",
                    "year": item.get("ProductionYear"),
                    "subtitle": str(item.get("ProductionYear") or ""),
                    "poster": item.get("Id"),
                    "versions": [],
                },
            )
            bucket["versions"].extend(self._versions(item))

        return self._finish(buckets)

    def scan_episodes(self, user_id: str, progress=None) -> List[Dict]:
        # Group at the individual-file level, not the Jellyfin-item level.
        # Jellyfin can misidentify obscure/unmatched anime and either (a)
        # split one true episode across several separate items, or (b) merge
        # several genuinely different episodes into one item's alternate
        # versions. Deriving season/episode straight from each file's own
        # path sidesteps both failure modes.
        flat = []
        seen = 0
        for item in self._items(user_id, "Episode", EPISODE_FIELDS):
            seen += 1
            if progress and seen % 200 == 0:
                progress(f"Reading episodes… {seen}")

            series_id = item.get("SeriesId") or _norm(item.get("SeriesName", ""))
            series_name = item.get("SeriesName") or "Unknown series"
            jf_season = item.get("ParentIndexNumber")
            jf_number = item.get("IndexNumber")
            item_name = item.get("Name")
            item_id = item.get("Id")

            for v in self._versions(item):
                season, number = resolve_season_episode(v["path"], jf_season, jf_number)
                if season is None or number is None:
                    continue
                flat.append((series_id, series_name, season, number, item_id, item_name, v))

        buckets: Dict[str, Dict] = {}
        for series_id, series_name, season, number, item_id, item_name, v in flat:
            key = f"{series_id}:S{season:02d}E{number:02d}"
            bucket = buckets.setdefault(
                key,
                {
                    "key": key,
                    "kind": "episode",
                    "title": item_name or f"Episode {number}",
                    "series": series_name,
                    "season": season,
                    "episode": number,
                    "subtitle": f"{series_name} · S{season:02d}E{number:02d}",
                    "poster": item_id,
                    "versions": [],
                },
            )
            bucket["versions"].append(v)

        return self._finish(buckets)

    @staticmethod
    def _finish(buckets: Dict[str, Dict]) -> List[Dict]:
        """Behåller bara grupper med fler än en fil och rangordnar dem."""
        groups = []
        for bucket in buckets.values():
            versions = [v for v in bucket["versions"] if v["path"]]
            # Samma fil kan dyka upp två gånger via sammanslagna versioner.
            unique = {}
            for v in versions:
                unique.setdefault(v["path"], v)
            versions = list(unique.values())
            if len(versions) < 2:
                continue

            versions.sort(
                key=lambda v: (v["height"] or 0, v["bitrate"] or 0, v["size"] or 0),
                reverse=True,
            )
            best = versions[0]
            for v in versions:
                v["best"] = v is best
            bucket["versions"] = versions
            bucket["totalSize"] = sum(v["size"] for v in versions)
            bucket["wastedSize"] = bucket["totalSize"] - best["size"]
            bucket["count"] = len(versions)
            # Identiska filer = säkrare att rensa, flagga det.
            heights = {v["height"] for v in versions}
            bucket["identical"] = len(heights) == 1
            groups.append(bucket)

        groups.sort(key=lambda g: g["wastedSize"], reverse=True)
        return groups

    # ---------- radering ----------

    def delete(self, item_id: str) -> None:
        url = f"{self.base_url}/Items/{item_id}"
        try:
            r = self.session.delete(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise JellyfinError(str(exc)) from exc
        if r.status_code == 401:
            raise JellyfinError("401 — nyckeln saknar behörighet.")
        if r.status_code == 403:
            raise JellyfinError(
                "403 — användaren bakom API-nyckeln får inte radera media. "
                "Slå på 'Allow media deletion' i Jellyfin."
            )
        if r.status_code >= 400:
            raise JellyfinError(f"Jellyfin svarade {r.status_code}")
