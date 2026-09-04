# R2B Video-Only Grading — Design

> **Status: shipped, with several decisions later reversed.** This is the
> design as written on 2026-07-10, kept unedited as a record of the
> reasoning. Where the code and this document disagree, the code is correct.
> Reversed since:
>
> - **Auto-disqualification (Goals, "score 0 across all 6 criteria" on
>   internal contradiction or fraud) was reversed** on 2026-07-12
>   after 7 out of 7 live auto-zeros proved to be false positives: currency
>   conversion, rounding, MRR versus sales, roadmap versus current status,
>   "250+" versus "300+", and conflicting stated goals. A surviving
>   inconsistency now lowers the specific criteria where the conflicting
>   claims live, is quoted in that criterion's rationale, and flags the row
>   for human review. Zeroing is a human decision.
>   `ProgramConfig.contradiction_auto_zero` is False for every program.
> - **The blank-video fallback (Error handling, "criterion-6 note, not a
>   hard failure") was reversed** on 2026-07-20. An R2B row with no
>   video link is now skipped entirely before any model call, with no
>   checkpoint entry, so it becomes eligible again the moment a link is
>   added.
> - **The notes column carries JSON, not the prose template** shown in
>   Design 3. It writes the same `criterion_scores` /
>   `criterion_rationale` blob the single-column programs write, plus
>   `confidence`.
> - **`write_multi_row_result` does not batch adjacent columns.** The eight
>   target columns are not guaranteed contiguous, and eight small Sheets
>   writes are negligible against the model calls, so each cell is written
>   individually.
> - **The no-show marker list grew from 6 to 10**, adding "not pitching"
>   and apostrophe-free variants, plus a punctuation-stripped comparison
>   pass.
> - **`_retry_without_video` no longer applies to R2B.** With video as the
>   only source there is nothing left to grade on, so the row fails and
>   falls through to the three-strikes escalation instead.
> - **The verify loop caps at 2 iterations, not 3**, for every program,
>   because both the analyst and the grader re-process the full video every
>   round.
> - **Criterion scores are rounded to 2 decimals**, not left unrounded as
>   Design 3 specifies.
> - **The Testing section's `run_one` helper was removed**; single-row
>   testing goes through `run_batch(target_row_number=...)` in the
>   dashboard.
> - **Identifier renames (not reversals):** the shared classes this design
>   calls `R2BGraderVerdict` and `R2BApprovalGate` are `GraderVerdict` and
>   `ApprovalGate` in the code today, renamed when the machinery stopped
>   being R2B-only.

## Context

R2B has been graded with a single combined score+reasoning column pair, the
same as Alchemist and Fellowship V2. The next R2B round uses a different
sheet layout (tab "AI") with a separate column per rubric criterion, a Total
Score column, and a Comments/Notes column — and grades from video evidence
only, ignoring the pitch deck link that's also present in the sheet.

## Goals

- Grade each startup on the 6 R2B rubric criteria (Problem & Solution,
  Market Potential, Product/MVP & Innovation, Team Strength, Business Model,
  Presentation & Clarity), writing each score to its own column.
- Analyze video only — the pitch deck link in the sheet is never fetched or
  used as evidence.
- Skip startups that didn't show up to pitch, detected automatically from
  the Startup Name text (no separate marker column).
- Disqualify (score 0 across all 6 criteria) on internal contradiction or
  fraud detected within the video itself.
- Reuse the existing Config/Checkpoint/Sheets infrastructure — sheet ID and
  tab ("AI") are entered through the app's existing sheet picker, no new
  env vars required.

## Non-goals

- No cross-source contradiction checking (there is no deck or separate
  application text to compare the video against for this round).
- No change to Alchemist or Fellowship V2's existing output format.
- No handling beyond "skip and pick up later" for startups whose Video cell
  is still blank — the user will populate all video links before running.

## Design

### 1. Video-only, deck-ignored (no code change needed)

`ProgramConfig.requires_pitch_deck` already defaults to `False` and R2B
doesn't set it — `process_row` already never calls `ingest_pitch_deck` for
R2B. The "Pitch decks" column is simply never read. `_submitted_video_url`
already finds the "Video" column by header-name substring match ("video"),
which works unchanged against this sheet's "Video" column.

R2B's prompts (analyst/grader/head) will be updated to remove any
deck-related fallback language, so the AI doesn't go looking for pitch-deck
evidence that was never attached.

### 2. Skip "didn't come" rows

New helper in `pipeline.py`, checked in `process_row` before any LLM call:

```python
_NO_SHOW_MARKERS = ("no response", "won't pitch", "didn't respond", "didn't come", "did not respond", "did not come")

def _is_no_show(startup_name: str) -> bool:
    name = _normalize_for_match(startup_name)
    return any(marker in name for marker in _NO_SHOW_MARKERS)
```

If the Startup Name cell (found the same way `name_idx` is found in
`app.py`) matches, `process_row` returns `(row_id, None)` immediately — same
"nothing to write" contract already used for already-checkpointed rows. No
checkpoint entry is written either, so removing the marker text later makes
the row eligible again on the next run.

### 3. Multi-column output

New `ProgramConfig` fields (used only when set — `None`/empty for existing
programs, so this doesn't touch Alchemist/Fellowship V2's code path):

```python
criterion_column_names: dict[str, str] | None = None   # {rubric criterion name: sheet header text}
total_score_column_name: str | None = None
notes_column_name: str | None = None
```

For this R2B round: `criterion_column_names` maps each of the 6
`RUBRIC_CRITERIA_R2B` entries to its sheet header (e.g. `"Problem & Solution"
-> "Problem & Solution (10 pts max)"`), `total_score_column_name = "Total
Score"`, `notes_column_name = "Comments / Notes"`.

New functions alongside the existing `resolve_output_columns`/
`write_row_result` in `google_clients.py`:

- `resolve_multi_output_columns(header, criterion_column_names,
  total_score_column_name, notes_column_name) -> dict` — same
  fail-loud-if-missing behavior as `resolve_output_columns`.
- `write_multi_row_result(sheets_service, sheet_id, sheet_name,
  sheet_row_number, col_map, criterion_scores: dict, total_score: float,
  notes: str)` — writes each criterion score to its own column, the Total
  Score, and the Comments/Notes text. Batches adjacent-column writes the
  same way `write_row_result` already does for score+reasoning, where
  possible.

`pipeline.py`'s `process_row`/`run_batch`/`run_one` branch on whether
`config.program_config.criterion_column_names` is set: if so, use the new
multi-column write path instead of the existing single score+reasoning path.

Values: criterion scores are the averaged (decimal, unrounded) per-criterion
scores already produced by `_average_head_samples`. Total Score = average of
the 6 criterion scores (decimal). Comments/Notes = the per-criterion
rationale already produced, reformatted as readable text:

```
Problem & Solution: <rationale>
Market Potential: <rationale>
...
```

instead of the JSON blob used elsewhere.

### 4. Disqualification (video-internal only)

`R2B_GRADER_INSTRUCTION` and `R2B_HEAD_INSTRUCTION` get a new section
(adapted from Alchemist's contradiction-detection language, but scoped down
since there's no second source to compare against): flag
`disqualifying_issue_found=true` only when the video's own claims are
internally inconsistent (e.g. two different revenue figures stated at
different points) or self-evidently fabricated — not based on comparing the
video to any external source. Uses the existing
`disqualifying_issue_found`/`disqualifying_issue_type`/
`disqualifying_issue_reason` fields already on `R2BGraderVerdict`, and the
existing override-to-0 logic in `workflow.py`'s `_average_head_samples` and
`agent.py`'s `R2BApprovalGate` — no schema changes needed, only prompt
changes.

### 5. Sampling

No change — keeps `N_SAMPLES=3` (existing default), same Head-averaging
architecture already used for Alchemist.

## Error handling

- Blank Video cell: `process_row`'s existing `video_error` /
  "video_primary" fallback (criterion 6 note, not a hard failure) already
  covers this — a row with no video yet just gets a low/flagged
  criterion-6 outcome rather than erroring. Since the user will populate
  all links before running, this is a safety net, not the expected path.
- Video fetch/processing failures reuse the existing generalized
  `_retry_without_video` retry-then-drop-video logic already shipped for
  Alchemist — for R2B, dropping the video leaves nothing to grade on, so a
  failure there falls through to the existing 3-strikes
  `FAILURE_ESCALATION_THRESHOLD` human-review escalation, same as any other
  program.

## Testing

- Unit-level: `_is_no_show` against the round's real Startup Name list to
  confirm exact matches with no false positives on real startup names.
- Live single-row test (`run_one`) against 2-3 real rows from the actual
  "AI" tab once the user has added video links, covering: a normal
  criterion-scored row, a no-show row (confirm skipped, nothing written),
  and (if available) a row expected to trigger disqualification.
- Confirm `resolve_multi_output_columns` fails loudly (not silently) if any
  of the 8 expected headers isn't found — matching existing
  `resolve_output_columns` behavior.
