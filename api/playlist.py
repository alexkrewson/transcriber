"""Enumerate the videos in a YouTube playlist.

Distillery's playlist mode needs a list of video IDs before it can ask
/api/fetch for each transcript. The obvious way to get one is the YouTube
Data API, but that means a Google Cloud project and an API key, and this
service already holds the thing that makes a key unnecessary: a Webshare
residential proxy. YouTube blocks Vercel's datacenter IPs, which is why
/api/fetch goes through the proxy -- the same proxy can load the ordinary
playlist page, which carries every video ID in its embedded ytInitialData.

So: no new credential, one more consumer of the proxy already paid for.
The cost is that this reads YouTube's internal page data, which YouTube can
reshape without notice. parse_playlist_page() is therefore written to search
the JSON for the renderers it wants rather than walking a fixed path, so a
layout change has to be fairly deep before it breaks. If it ever does break,
the Data API remains a drop-in replacement for this file alone.
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from youtube_transcript_api.proxies import WebshareProxyConfig
import json
import os
import re
import requests

# A playlist page ships its first ~100 videos inline and hides the rest behind
# continuation tokens, one round trip per further ~100. Each trip goes through
# a rotating residential proxy and Vercel will kill the function long before an
# unbounded playlist finishes, so stop at a number that fits the time budget.
# The response says when it stopped early rather than pretending it is complete.
DEFAULT_MAX_VIDEOS = 200
HARD_MAX_VIDEOS = 500

# A playlist page served to a browser-looking client. Without a UA YouTube
# serves a consent interstitial that carries no ytInitialData at all.
PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Playlists whose id starts with RD are Mixes: generated per viewer, endless,
# and with no fixed membership to enumerate. Better to say so than to return
# the arbitrary handful of videos the page happens to contain for this request.
MIX_PREFIX = "RD"

# Entries for videos that no longer exist still occupy a slot in the playlist.
# They have a real videoId but no transcript will ever be available.
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}


def extract_playlist_id(url: str) -> str:
    """Pull a playlist id out of any of the forms someone might paste."""
    match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{12,}", url):
        return url  # already a bare id
    raise ValueError("Could not find a playlist id in: " + url)


def _proxies() -> dict:
    return WebshareProxyConfig(
        proxy_username=os.environ["WEBSHARE_PROXY_USERNAME"],
        proxy_password=os.environ["WEBSHARE_PROXY_PASSWORD"],
    ).to_requests_dict()


def _extract_json_object(html: str, marker: str):
    """Return the JSON object that starts at the first brace after `marker`.

    A regex cannot do this: the object contains braces inside strings, and the
    trailing text differs between page variants. Counting braces while tracking
    string state is the only version that does not silently truncate.
    """
    start = html.find(marker)
    if start == -1:
        return None
    start = html.find("{", start)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _find_all(node, key):
    """Yield every value stored under `key` anywhere in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            else:
                for found in _find_all(v, key):
                    yield found
    elif isinstance(node, list):
        for item in node:
            for found in _find_all(item, key):
                yield found


def _first(node, key):
    for value in _find_all(node, key):
        return value
    return None


def _text(node) -> str:
    """Read YouTube's several interchangeable text shapes.

    Three coexist on a single page: the legacy {simpleText} and {runs: [...]},
    and {content} from the newer ViewModel components.
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    if "content" in node and isinstance(node["content"], str):
        return node["content"]
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return ""


def parse_playlist_page(data) -> dict:
    """Extract videos, playlist title and continuation token from ytInitialData.

    Pure: takes already-parsed JSON, so it can be tested against a saved page.

    YouTube is mid-migration from its old renderer components to newer
    ViewModel ones, and which of the two a playlist page uses varies by
    playlist and changes over time. Both shapes are read here, and results are
    deduplicated by video id, so a page serving either -- or a mix of the two
    during the changeover -- enumerates correctly.
    """
    videos = []
    seen = set()
    skipped = 0

    def add(video_id, title):
        nonlocal skipped
        if not video_id or video_id in seen:
            return
        if title.strip().lower() in UNAVAILABLE_TITLES:
            seen.add(video_id)
            skipped += 1
            return
        seen.add(video_id)
        videos.append({"videoId": video_id, "title": title})

    # Newer layout: a lockup per playlist row, holding the id directly.
    for lockup in _find_all(data, "lockupViewModel"):
        if not isinstance(lockup, dict):
            continue
        if lockup.get("contentType") not in (None, "LOCKUP_CONTENT_TYPE_VIDEO"):
            continue
        metadata = _first(lockup, "lockupMetadataViewModel")
        title = _text(metadata.get("title")) if isinstance(metadata, dict) else ""
        add(lockup.get("contentId"), title)

    # Legacy layout, still served for some playlists.
    for renderer in _find_all(data, "playlistVideoRenderer"):
        if not isinstance(renderer, dict):
            continue
        add(renderer.get("videoId"), _text(renderer.get("title")))

    token = None
    for key in ("continuationItemViewModel", "continuationItemRenderer"):
        for cont in _find_all(data, key):
            candidate = _first(cont, "token")
            if candidate:
                token = candidate
                break
        if token:
            break

    playlist_title = ""
    for key in ("playlistHeaderRenderer", "playlistMetadataRenderer"):
        header = _first(data, key)
        if isinstance(header, dict):
            playlist_title = _text(header.get("title"))
            if playlist_title:
                break

    return {
        "videos": videos,
        "skipped": skipped,
        "continuation": token,
        "playlistTitle": playlist_title,
    }


def _fetch_continuation(session, token, api_key, client_version):
    """Ask YouTube's internal browse endpoint for the next page of the playlist."""
    url = "https://www.youtube.com/youtubei/v1/browse"
    if api_key:
        url += "?key=" + api_key
    body = {
        "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
        "continuation": token,
    }
    resp = session.post(url, json=body, headers=PAGE_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_playlist(playlist_id: str, max_videos: int) -> dict:
    if playlist_id.startswith(MIX_PREFIX):
        raise ValueError(
            "That is a YouTube Mix, not a playlist. Mixes are generated per "
            "viewer and have no fixed list of videos to download."
        )

    session = requests.Session()
    session.proxies.update(_proxies())

    resp = session.get(
        "https://www.youtube.com/playlist?list=" + playlist_id,
        headers=PAGE_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    html = resp.text

    data = _extract_json_object(html, "ytInitialData")
    if data is None:
        raise RuntimeError(
            "YouTube did not return playlist data. The playlist may be private, "
            "or YouTube changed its page format."
        )

    result = parse_playlist_page(data)
    videos = result["videos"]
    skipped = result["skipped"]
    token = result["continuation"]

    if not videos and not skipped:
        alert = _first(data, "alertRenderer")
        message = _text(alert.get("text")) if isinstance(alert, dict) else ""
        raise RuntimeError(message or "No videos found in that playlist.")

    api_key = None
    match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    if match:
        api_key = match.group(1)
    client_version = "2.20240101.00.00"
    match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    if match:
        client_version = match.group(1)

    truncated = False
    while token and len(videos) < max_videos:
        try:
            page = _fetch_continuation(session, token, api_key, client_version)
        except Exception:
            # A failed continuation means an incomplete list, not a failed
            # request -- return what we have and say it is short.
            truncated = True
            break
        parsed = parse_playlist_page(page)
        if not parsed["videos"]:
            break
        videos.extend(parsed["videos"])
        skipped += parsed["skipped"]
        token = parsed["continuation"]

    if token and len(videos) >= max_videos:
        truncated = True
    videos = videos[:max_videos]

    return {
        "playlistId": playlist_id,
        "playlistTitle": result["playlistTitle"] or playlist_id,
        "videos": videos,
        "skipped": skipped,
        "truncated": truncated,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        url = params.get("url", [None])[0]

        if not url:
            self._respond(400, {"error": "Missing 'url' query parameter"})
            return

        try:
            max_videos = int(params.get("max", [DEFAULT_MAX_VIDEOS])[0])
        except ValueError:
            max_videos = DEFAULT_MAX_VIDEOS
        max_videos = max(1, min(max_videos, HARD_MAX_VIDEOS))

        try:
            playlist_id = extract_playlist_id(url)
        except ValueError as e:
            self._respond(400, {"error": str(e)})
            return

        try:
            self._respond(200, fetch_playlist(playlist_id, max_videos))
        except ValueError as e:
            self._respond(400, {"error": str(e)})
        except RuntimeError as e:
            self._respond(404, {"error": str(e)})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format, *args):
        pass
