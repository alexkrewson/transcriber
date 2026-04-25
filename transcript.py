#!/usr/bin/env python3
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

os.makedirs("transcriptions", exist_ok=True)
logging.basicConfig(
    filename=os.path.join("transcriptions", "errors.log"),
    level=logging.ERROR,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


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
    lines = []
    current_bucket = -1
    bucket_text = []

    for entry in entries:
        bucket = int(entry.start // 30)
        if bucket != current_bucket:
            if bucket_text:
                lines.append(f"[{format_time(current_bucket * 30)}] {' '.join(bucket_text)}")
            current_bucket = bucket
            bucket_text = [entry.text.lower()]
        else:
            bucket_text.append(entry.text.lower())

    if bucket_text:
        lines.append(f"[{format_time(current_bucket * 30)}] {' '.join(bucket_text)}")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 transcript.py <youtube-url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]

    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        logging.error("Bad URL %s — %s", url, e)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        text = fetch_transcript(video_id)
        print(text)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = os.path.join("transcriptions", f"transcript_{video_id}_{timestamp}.txt")
        with open(filename, "w") as f:
            f.write(text)
        print(f"Saved to {filename}", file=sys.stderr)
        file_uri = f"copy\nfile://{os.path.abspath(filename)}".encode()
        subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "x-special/gnome-copied-files"],
            input=file_uri
        )
        print("File copied to clipboard", file=sys.stderr)
        subprocess.Popen(["xdg-open", os.path.abspath("transcriptions")])
    except TranscriptsDisabled:
        logging.error("Transcripts disabled for video %s", video_id)
        print("Error: transcripts are disabled for this video.", file=sys.stderr)
        sys.exit(1)
    except NoTranscriptFound:
        logging.error("No transcript found for video %s", video_id)
        print("Error: no transcript found for this video.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logging.exception("Unexpected error for video %s — %s", video_id, e)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
