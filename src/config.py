"""
Central config, env-driven only — no hardcoded secrets or sheet IDs.
Fail fast at startup (not mid-batch) if required env vars are missing,
since a 100+ row run that dies on row 50 from a bad API key wastes
real API spend.

Multiple programs coexist (Fellowship V2, R2B, Alchemist). The active
program is selected via the ``PROGRAM`` env var (default
``fellowship_v2``); each program owns its own sheet-geometry env-var
names so the sheets never collide. A single run_batch call is exactly one
program — all rows in a batch share the same criteria set, so the
module-level active-criteria slot set in schemas.py never races across
programs within a batch.

Gemini-only for now (no Claude credit on this account) — see project
memory for the multi-provider history if Claude support needs reviving.
"""
import dataclasses
import os
from dataclasses import dataclass

from .programs import ProgramConfig, get_program_config


@dataclass(frozen=True)
class Config:
    sheet_id: str
    sheet_range: str
    header_row: int
    top_label_row: int
    service_account_path: str
    analyzer_model: str
    grader_model: str
    head_model: str
    n_samples: int
    max_concurrency: int
    checkpoint_path: str
    program_config: ProgramConfig

    @classmethod
    def from_env(
        cls,
        program: str,
        *,
        sheet_id_override: str | None = None,
        sheet_range_override: str | None = None,
        header_row_override: int | None = None,
        top_label_row_override: int | None = None,
        score_column_override: str | None = None,
        reasoning_column_override: str | None = None,
        ignored_columns_override: tuple[str, ...] | None = None,
    ) -> "Config":
        """Build Config from env vars, with optional per-call overrides for
        sheet_id/sheet_range/header_row/top_label_row — used by app.py's
        sheet picker so a user can point a run at a different sheet/tab/
        header row (e.g. R2B's multiple competition rounds, each its own
        sheet, sometimes with no merged top-label row at all) without
        editing .env. Omitted overrides fall back to the existing env-var
        behavior unchanged.

        score_column_override/reasoning_column_override/ignored_columns_override
        let the caller replace which sheet columns this run writes score/
        reasoning to and which columns are hidden from the AI, instead of
        the hardcoded per-program values in ProgramConfig (Alchemist's
        column layout isn't fixed across sheets the way R2B's is — see
        app.py's per-session column-mapping dropdowns, Alchemist-only for
        now). Building a derived ProgramConfig via dataclasses.replace here
        keeps this scoped to a single Config instance/run — the shared
        ALCHEMIST_CONFIG singleton in programs.py is never mutated, so a
        concurrent run using default columns is unaffected.
        """
        program_config = get_program_config(program)
        if score_column_override or reasoning_column_override or ignored_columns_override is not None:
            excluded_names = frozenset(ignored_columns_override or ()) | {""}
            if score_column_override:
                excluded_names = excluded_names | {score_column_override}
            if reasoning_column_override:
                excluded_names = excluded_names | {reasoning_column_override}
            program_config = dataclasses.replace(
                program_config,
                score_column_name=score_column_override or program_config.score_column_name,
                reasoning_column_name=(
                    reasoning_column_override or program_config.reasoning_column_name
                ),
                # Replaces the hardcoded excluded_header_names/substrings
                # entirely rather than adding to them: once the UI shows
                # every real column and the user picks exactly which ones
                # to ignore, a leftover hardcoded substring match (e.g.
                # "ceo") could silently hide a column the user chose to
                # keep visible to the AI.
                excluded_header_names=excluded_names,
                excluded_header_substrings=(),
            )
        sheet_id = sheet_id_override or os.environ.get(program_config.sheet_id_env, "")
        sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "")

        if not sheet_id:
            raise RuntimeError(f"{program_config.sheet_id_env} not set")
        if not sa_path or not os.path.isfile(sa_path):
            raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_PATH invalid: {sa_path}")
        use_vertex = os.environ.get(
            "GOOGLE_GENAI_USE_VERTEXAI",
            "FALSE",
        ).upper() == "TRUE"
        if use_vertex:
            if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
                raise RuntimeError("GOOGLE_CLOUD_PROJECT not set for Vertex AI")
            if not os.environ.get("GOOGLE_CLOUD_LOCATION"):
                raise RuntimeError("GOOGLE_CLOUD_LOCATION not set for Vertex AI")
        elif not os.environ.get("GOOGLE_API_KEY"):
            raise RuntimeError("GOOGLE_API_KEY not set for Gemini Developer API")

        # No hardcoded model-name fallback: which Gemini tier to run is a
        # cost/quality tradeoff (a prior commit silently switched every
        # model from gemini-3.5-flash to the cheaper-but-less-reliable
        # gemini-3.1-flash-lite), not something safe to default silently.
        # Every deployment must say explicitly which model it wants, same
        # as it must say which sheet/service account to use.
        analyzer_model = os.environ.get("ANALYZER_MODEL", "")
        grader_model = os.environ.get("GRADER_MODEL", "")
        if not analyzer_model:
            raise RuntimeError("ANALYZER_MODEL not set")
        if not grader_model:
            raise RuntimeError("GRADER_MODEL not set")
        # HEAD_MODEL defaulting to GRADER_MODEL is an intentional, documented
        # choice (workflow.py: "Head model defaults to the grader model"),
        # not a hidden fallback — both are already known-explicit by here.
        head_model = os.environ.get("HEAD_MODEL", grader_model)

        sheet_range = sheet_range_override or os.environ.get(
            program_config.sheet_range_env, "Grading Final",
        )
        header_row = (
            header_row_override
            if header_row_override is not None
            else int(os.environ.get(
                program_config.header_row_env, str(program_config.default_header_row),
            ))
        )

        # Checkpoint is scoped per (program, sheet_id) — a single shared
        # checkpoint would incorrectly treat "already graded in sheet A" as
        # "already graded in sheet B" for the same applicant email, once
        # sheet switching is dynamic (e.g. R2B's multiple competition
        # rounds, each its own sheet). _derive_row_id in pipeline.py keys
        # checkpoints by email only, with no sheet_id in the key, so this
        # isolation has to happen at the file level instead.
        #
        # Every program (including Alchemist, as of this session) uses this
        # auto-generated path uniformly — no CHECKPOINT_PATH override. A
        # prior Alchemist-only exception here (honoring a global
        # CHECKPOINT_PATH env var) caused a real bug: since that var has no
        # program name in it, honoring it for every program made
        # fellowship_v2/r2b silently pick up ALCHEMIST's checkpoint file
        # too. Scoping it to "alchemist only" fixed that particular bug but
        # kept the underlying fragility (a single global var, easy to
        # forget/misconfigure); several inconsistent, conflicting
        # checkpoint_alchemist*.json files accumulated in /tmp as a result,
        # which is why they were deleted rather than migrated.
        import hashlib
        sheet_hash = hashlib.sha1(sheet_id.encode()).hexdigest()[:10]
        checkpoint_path = f"checkpoint_{program}_{sheet_hash}.json"

        return cls(
            sheet_id=sheet_id,
            sheet_range=sheet_range,
            header_row=header_row,
            top_label_row=(
                top_label_row_override
                if top_label_row_override is not None
                else int(os.environ.get(
                    program_config.top_label_row_env, str(program_config.default_top_label_row),
                ))
            ),
            service_account_path=sa_path,
            analyzer_model=analyzer_model,
            grader_model=grader_model,
            head_model=head_model,
            n_samples=int(os.environ.get("N_SAMPLES", "3")),
            max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "8")),
            checkpoint_path=checkpoint_path,
            program_config=program_config,
        )
