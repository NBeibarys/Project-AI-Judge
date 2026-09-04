"""Go/no-go check for round-video segment clipping (operator-entered timestamps).

Usage:
    python scripts/verify_youtube_clipping.py <youtube_url> <start like 3:12> <end like 9:48>

Asks Gemini to describe ONLY the clipped span of the given YouTube video.
PASS = the response clearly describes content from that span (operator
judges by watching those minutes in the YouTube player).
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from google import genai
from google.genai import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.round_segments import parse_timestamp


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    url, start_raw, end_raw = sys.argv[1], sys.argv[2], sys.argv[3]
    start_s, end_s = parse_timestamp(start_raw), parse_timestamp(end_raw)
    client = genai.Client()  # env-driven: GOOGLE_GENAI_USE_VERTEXAI etc.
    response = client.models.generate_content(
        model=os.environ["ANALYZER_MODEL"],
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        file_data=types.FileData(file_uri=url, mime_type="video/mp4"),
                        video_metadata=types.VideoMetadata(
                            start_offset=f"{start_s}s",
                            end_offset=f"{end_s}s",
                        ),
                    ),
                    types.Part(
                        text=(
                            "Describe what happens in this video clip in 5 sentences: "
                            "who is speaking, what company/product is discussed, and "
                            "roughly what is said first and last."
                        )
                    ),
                ],
            )
        ],
        config=types.GenerateContentConfig(temperature=0.0),
    )
    print(response.text)


if __name__ == "__main__":
    main()
