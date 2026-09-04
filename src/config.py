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


def _env(name: str, default: str) -> str:
    """Read an env var, treating a set-but-empty value as unset.

    .env.example ships several settings as bare ``NAME=`` placeholders and
    dotenv loads those as empty strings, so a plain ``os.environ.get(name,
    default)`` hands the rest of this module an empty model name or sheet
    range instead of the documented default. Surrounding whitespace is
    stripped for the same reason: a stray space in a copy-pasted value is
    an operator typo, never a meaningful setting.
    """
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    """_env for integer settings, naming the variable in the error.

    Bare ``int("")`` raises "invalid literal for int() with base 10: ''",
    which tells an operator nothing about which of the several
    sheet-geometry variables they left half-filled.
    """
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}.") from exc


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
        name_column_override: str | None = None,
        ignored_columns_override: tuple[str, ...] | None = None,
        criterion_column_overrides: dict[str, str] | None = None,
        total_score_column_override: str | None = None,
        notes_column_override: str | None = None,
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
        the hardcoded per-program values in ProgramConfig (Alchemist's and
        Fellowship V2's column layout isn't fixed across sheets — see
        app.py's per-session column-mapping dropdowns). Building a derived
        ProgramConfig via dataclasses.replace here keeps this scoped to a
        single Config instance/run — the shared ALCHEMIST_CONFIG/
        FELLOWSHIP_V2_CONFIG singleton in programs.py is never mutated, so a
        concurrent run using default columns is unaffected.

        criterion_column_overrides/total_score_column_override/
        notes_column_override are the equivalent for the MULTI-column output
        shape (R2B today: one sheet column per rubric criterion, plus a
        Total Score column and a Notes/Comments column, instead of a single
        score+reasoning pair). Same non-mutating dataclasses.replace
        approach — the shared R2B_CONFIG singleton is never mutated.

        name_column_override selects which sheet column holds the
        applicant/startup name (used for no-show detection, R2B's wrong-
        segment tripwire note, and duplicate-email disambiguation — see
        pipeline.py's _resolve_name_column_index/_derive_row_id), instead
        of pipeline.py guessing it via a hardcoded substring hint list.
        Applied independently of the single-column vs multi-column split
        above (the name column matters for both output shapes), via its
        own dataclasses.replace on whatever program_config already is by
        that point — so it composes with either branch above.
        """
        program_config = get_program_config(program)
        if program_config.criterion_column_names is not None:
            # Multi-column shape (R2B): one sheet column per rubric
            # criterion, plus Total Score/Notes columns, instead of a
            # single score+reasoning pair. Mirrors the single-column branch
            # below, just extended to N criterion columns.
            if (
                criterion_column_overrides
                or total_score_column_override
                or notes_column_override
                or ignored_columns_override is not None
            ):
                new_criterion_column_names = dict(program_config.criterion_column_names)
                if criterion_column_overrides:
                    new_criterion_column_names.update(criterion_column_overrides)
                new_total_score_column_name = (
                    total_score_column_override or program_config.total_score_column_name
                )
                new_notes_column_name = (
                    notes_column_override or program_config.notes_column_name
                )
                excluded_names = (
                    frozenset(ignored_columns_override or ())
                    | {""}
                    | frozenset(new_criterion_column_names.values())
                )
                if new_total_score_column_name:
                    excluded_names = excluded_names | {new_total_score_column_name}
                if new_notes_column_name:
                    excluded_names = excluded_names | {new_notes_column_name}
                program_config = dataclasses.replace(
                    program_config,
                    criterion_column_names=new_criterion_column_names,
                    total_score_column_name=new_total_score_column_name,
                    notes_column_name=new_notes_column_name,
                    # Same rationale as the single-column path below: replace
                    # the hardcoded excluded set entirely rather than adding
                    # to it, now that the UI shows every real column and the
                    # user has explicitly mapped/ignored what they want.
                    excluded_header_names=excluded_names,
                    excluded_header_substrings=(),
                )
        elif score_column_override or reasoning_column_override or ignored_columns_override is not None:
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
        if name_column_override:
            # Independent of the single-column vs multi-column split above:
            # the applicant-name column matters for both output shapes, so
            # this is its own top-level check rather than living inside
            # either branch (which would silently drop it for the other
            # shape). Replaces whatever program_config already is by this
            # point — composes correctly whether or not one of the branches
            # above already produced a derived ProgramConfig.
            program_config = dataclasses.replace(
                program_config, name_column_name=name_column_override,
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
        head_model = _env("HEAD_MODEL", grader_model)

        sheet_range = sheet_range_override or _env(
            program_config.sheet_range_env, "Grading Final",
        )
        header_row = (
            header_row_override
            if header_row_override is not None
            else _env_int(
                program_config.header_row_env, program_config.default_header_row,
            )
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
        # program_config.program, not the raw argument: get_program_config
        # normalizes case and whitespace, so PROGRAM="R2B " and "r2b" must
        # not end up with two checkpoint files for the same sheet (the
        # second run would silently re-grade every row at full API cost).
        checkpoint_path = f"checkpoint_{program_config.program}_{sheet_hash}.json"

        return cls(
            sheet_id=sheet_id,
            sheet_range=sheet_range,
            header_row=header_row,
            top_label_row=(
                top_label_row_override
                if top_label_row_override is not None
                else _env_int(
                    program_config.top_label_row_env,
                    program_config.default_top_label_row,
                )
            ),
            service_account_path=sa_path,
            analyzer_model=analyzer_model,
            grader_model=grader_model,
            head_model=head_model,
            n_samples=_env_int("N_SAMPLES", 3),
            # 3, not 8: 8 caused sustained 429s in a real 100-row run
            # (69/100 rows failed) — see .env.example's MAX_CONCURRENCY note.
            # The default must be the value proven safe in production, since
            # an operator who never sets the var gets exactly this one.
            max_concurrency=_env_int("MAX_CONCURRENCY", 3),
            checkpoint_path=checkpoint_path,
            program_config=program_config,
        )
