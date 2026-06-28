# AI Fellowship Agent

An evidence-grounded Google ADK workflow that reviews fellowship applications
from Google Sheets using submitted text and public video links.

## Review flow

1. The analyst extracts evidence for all nine rubric dimensions.
2. The grader-head verifies every claim against the original multimodal input.
3. An approved verdict is written to Sheets.
4. A rejected analysis receives one revision; a second rejection writes a blank
   grade and a `[NEEDS HUMAN REVIEW]` marker in AI Reasoning.

The normal path makes two model calls. The single-revision path makes four.
Both roles default to the stable `gemini-3.5-flash` model.

## Video handling

The application never downloads video bodies. It passes native YouTube links
directly to Gemini and resolves other public HTTPS links through response
headers plus bounded OpenGraph, Twitter, HTML video, and JSON-LD metadata.
Unknown webpages are interpreted by the analyst through Gemini URL Context;
the grader independently receives the same source. Private-network URLs,
unsupported formats, unsafe redirects, and external files over 100 MB fail
closed.

## Local setup

Use Python 3.12 and install the validated direct dependency versions:

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Configure the Gemini key, service-account file, Sheet ID, and sheet geometry in
`.env`. Share the target Sheet with the service-account email as an editor.

Run the checkpointed batch:

```powershell
python -m src.main
```

Grades are checkpointed only after the corresponding Sheets write succeeds.
Checkpoint keys are pseudonymized, and grade narratives are not duplicated
locally.
