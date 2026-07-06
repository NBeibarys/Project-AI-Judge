# AI Fellowship Agent

An evidence-grounded Google ADK workflow that reviews fellowship and R2B
(Road 2 Battlefield) applications from Google Sheets using submitted text
and public video links.

## Programs

Two grading programs coexist, selected via the `PROGRAM` env var:

| Program | Criteria | Bands | Source priority | Video |
|---------|----------|-------|-----------------|-------|
| `fellowship` (default) | 9 | 5-band (1-2/3-4/5-6/7-8/9-10) | Text-primary | Supplementary |
| `r2b` | 6 | 3-band (1-3/4-6/7-10) | Video-primary | Mandatory (criterion 6) |

## Review flow

The two programs use different agent architectures:

### Fellowship (2 agents: analyst + grader)

1. The **analyst** (LLM) extracts evidence for all rubric dimensions.
2. The **grader** (LLM) independently verifies AND scores in one step
   (holistic 0-10 score). The **ApprovalGate** (zero-model) publishes the
   final score on approval, or sends the analyst feedback for one revision.
   After 2 rejections, the row escalates to human review.
3. Single run; no multi-sample averaging.

### R2B (3 agents: analyst + grader + head)

1. The **analyst** (LLM) extracts evidence per criterion from the video.
   Does NOT score.
2. The **grader** (LLM, temp=0) VERIFIES the analyst evidence only — checks
   it is real, grounded, and covers all 6 criteria (with video evidence for
   criterion 6). Does NOT score. If rejected, the analyst revises. Max 3
   iterations. The **R2BApprovalGate** exits the loop on approval without
   scoring.
3. The **head** (LLM, temp=0) SCORES the approved evidence only — 1-10 per
   criterion using band-then-integer, writes rationale BEFORE score
   (rationale-before-score CoT), final score = average of 6 criterion scores.
   A minor ±1 adjustment is allowed with reasoning. Verbosity guard: scores
   evidence quality, not video length.

### Multi-sample averaging (R2B only)

The analyst->grader verify loop runs ONCE. Then the Head scorer is re-run
`N_SAMPLES` times (default 3) at temp=0 on the SAME approved evidence. The
6 criterion scores are averaged across runs, and the rationale is selected
from the run whose final score is closest to the average (closest-rationale
selection). This reduces run-to-run variance (arXiv:2606.26185 — temp=0 is
not sufficient for determinism) without re-running the expensive verify loop.

Failed Head runs (API error, schema validation) are excluded from the
average. If all runs fail, the row escalates to human review.

Cost: the verify loop makes 2-6 calls (analyst + grader, up to 3 iterations).
The Head makes `N_SAMPLES` calls (default 3). Total: 5-9 calls per applicant.

## Video handling

**Fellowship** (text-primary): passes native YouTube links directly to Gemini
and resolves other public HTTPS links through response headers plus bounded
OpenGraph, Twitter, HTML video, and JSON-LD metadata. Unknown webpages are
interpreted by the analyst through Gemini URL Context. Private-network URLs,
unsupported formats, unsafe redirects, and external files over 100 MB fail
closed.

**R2B** (video-primary): uses Tier-2 video ingestion (`video_ingestion.py`)
that can download Google Drive share links and large HTTPS videos, then
upload them to the Gemini Files API so the analyst and head receive the
video as a native multimodal Part. Criterion 6 (Presentation & Clarity)
requires video evidence — if no video is available, it scores 1 with
rationale "No video submitted."

## Local setup

Use Python 3.12 and install the validated direct dependency versions:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
```

Configure the Gemini Developer API key and/or Vertex AI project alongside the
service-account file, Sheet ID, and sheet geometry in `.env`. Select Vertex with
`GOOGLE_GENAI_USE_VERTEXAI=TRUE`; otherwise the API-key backend remains active.
Share the target Sheet with the service-account email as an editor.

For R2B, also share the service-account email with any Google Drive videos
(the Drive download needs `drive.readonly` scope, built from the same JSON).

Run the checkpointed batch:

```bash
PROGRAM=r2b python -m src.main
```

Grades are checkpointed only after the corresponding Sheets write succeeds.
Checkpoint keys are pseudonymized, and grade narratives are not duplicated
locally.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROGRAM` | `fellowship` | Which grading program to run |
| `FELLOWSHIP_SHEET_ID` | — | Fellowship Google Sheet ID |
| `R2B_SHEET_ID` | — | R2B Google Sheet ID |
| `ANALYZER_MODEL` | `gemini-3.5-flash` | Model for the analyst agent |
| `GRADER_MODEL` | `gemini-3.5-flash` | Fellowship grader (verify+score) or R2B grader (verify only) |
| `HEAD_MODEL` | (same as GRADER_MODEL) | R2B Head scorer (scores approved evidence) |
| `N_SAMPLES` | `3` | R2B: number of Head runs to average |
| `MAX_CONCURRENCY` | `4` | Max parallel applicants |
| `CHECKPOINT_PATH` | `checkpoint.json` | Checkpoint file path |
