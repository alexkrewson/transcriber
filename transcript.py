#!/usr/bin/env python3
import re
import sys
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled


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


def fetch_transcript(video_id: str) -> str:
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
    return "\n".join(f"[{format_time(entry.start)}] {entry.text.lower()}" for entry in entries)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 transcript.py <youtube-url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]

    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        text = fetch_transcript(video_id)
        print(text)
    except TranscriptsDisabled:
        print("Error: transcripts are disabled for this video.", file=sys.stderr)
        sys.exit(1)
    except NoTranscriptFound:
        print("Error: no transcript found for this video.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
