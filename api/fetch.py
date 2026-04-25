from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
import json
import re


def extract_video_id(url: str) -> str:
    patterns = [
        r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract video ID from: {url}")


def format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def fetch_transcript(video_id: str) -> list:
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)

    try:
        transcript = transcript_list.find_manually_created_transcript(["en", "en-US", "en-GB"])
    except NoTranscriptFound:
        try:
            transcript = transcript_list.find_generated_transcript(["en", "en-US", "en-GB"])
        except NoTranscriptFound:
            transcript = next(iter(transcript_list))

    entries = transcript.fetch()
    return [{"time": format_time(e.start), "text": e.text.lower()} for e in entries]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        url = params.get("url", [None])[0]

        if not url:
            self._respond(400, {"error": "Missing 'url' query parameter"})
            return

        try:
            video_id = extract_video_id(url)
        except ValueError as e:
            self._respond(400, {"error": str(e)})
            return

        try:
            entries = fetch_transcript(video_id)
            self._respond(200, {"entries": entries})
        except TranscriptsDisabled:
            self._respond(400, {"error": "Transcripts are disabled for this video."})
        except NoTranscriptFound:
            self._respond(404, {"error": "No transcript found for this video."})
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
