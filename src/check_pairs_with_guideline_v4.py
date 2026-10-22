import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(BASE_DIR),
)


from config import (
    OUTPUT_DATA_DIR,
)

from src.generator import (
    create_llm,
    clean_json_response,
    read_text_file,
)


# ============================================================
# Check pairs with matching guideline v0.4
#
# GPT-4o judges each pair with prompts/matching_guidelines_v0.5.md
# as system prompt (temperature 0, one pair per call) and returns
# decision, confidence, evidence and reason.
#
# Decisions GENERIC / DIRECT / RESIDUAL -> Match, NO -> No match.
# PLAUSIBLE -> Match for positive and non-lexical pairs, Excluded
# (left out of training) for easy and semi-hard negatives.
# Confidence is kept, not used for the label.
#
# Input: the 100 or 300 review pairs (--sample), compared with
# the reference labels (manual_review_label when filled, otherwise
# our_label where the blind reviewer agreed; unjudged
# disagreements are Pending and not scored).
#
# Each answer is appended to a checkpoint CSV, so a re-run only
# calls the API for pairs that are not done yet.
# Input files are not modified.
# ============================================================

GUIDELINE_PATH = BASE_DIR / "prompts" / "matching_guidelines_v0.5.md"

PENDING = "Pending"


def sample_paths(sample: int) -> tuple[Path, Path]:
    """
    Blind input and comparison (reference) file of a review sample.
    """

    return (
        OUTPUT_DATA_DIR / f"review_{sample}_pairs_without_labels.xlsx",
        OUTPUT_DATA_DIR / f"review_{sample}_pairs_comparison.xlsx",
    )


def run_paths(sample: int, run: str | None) -> tuple[Path, Path]:
    """
    Checkpoint and output paths of one run. Without a run name the
    names of the first run are used, e.g. with sample 100 and "run2":
    review_100_pairs_gpt4o_v04_run2_checkpoint.csv
    """

    prefix = f"review_{sample}_pairs_gpt4o_v04" + (f"_{run}" if run else "")

    return (
        OUTPUT_DATA_DIR / f"{prefix}_checkpoint.csv",
        OUTPUT_DATA_DIR / f"{prefix}_decisions.xlsx",
    )

MAX_LLM_RETRIES = 3

RETRY_DELAY_SECONDS = 10

DECISION_TO_LABEL = {
    "GENERIC": "Match",
    "DIRECT": "Match",
    "RESIDUAL": "Match",
    "NO": "No match",
}

EXCLUDED = "Excluded"

OUTPUT_COLUMNS = [
    "review_id",
    "source_pair_id",
    "pair_category",
    "client_expense_title",
    "client_expense_description",
    "expense_class",
    "program_category",
    "program_subcategory",
    "program_expense_description",
    "reference_label",
    "v04_label",
    "agreement",
    "v04_decision",
    "v04_confidence",
    "v04_reason",
    "v04_client_evidence",
    "v04_program_evidence",
    "evidence_problem",
]


# ============================================================
# Response model (output format of the guideline)
# ============================================================

class Evaluation(BaseModel):
    program_expense_id: str
    client_evidence: str | None
    program_evidence: str
    decision: Literal["GENERIC", "DIRECT", "PLAUSIBLE", "RESIDUAL", "NO"]
    confidence: Literal["high", "medium", "low"]
    reason: str


class Result(BaseModel):
    client_expense_id: str
    evaluations: list[Evaluation]


class GuidelineResponse(BaseModel):
    results: list[Result]


# ============================================================
# Helpers
# ============================================================

def clean_text(value) -> str:
    """
    One line, single spaces, no table separators.
    The same text is sent to the LLM and used for the evidence check.
    """

    if pd.isna(value):
        return ""

    text = str(value).replace("|", "/")

    return re.sub(r"\s+", " ", text).strip()


def load_system_prompt() -> str:
    """
    The guideline without its leading HTML comment (notes for humans).
    """

    text = read_text_file(GUIDELINE_PATH)

    return re.sub(r"^\s*<!--.*?-->\s*", "", text, flags=re.DOTALL)


def build_user_message(row: pd.Series) -> str:
    """
    One client expense and one candidate, in the <input> format
    of the guideline.
    """

    return "\n".join([
        f"## Client expense `{row['review_id']}`",
        "",
        f"- **title:** {clean_text(row['client_expense_title'])}",
        f"- **description:** {clean_text(row['client_expense_description'])}",
        "",
        "### Candidates",
        "",
        "| id | program | category | subcategory | description |",
        "|---|---|---|---|---|",
        (
            f"| P1 |  | {clean_text(row['program_category'])}"
            f" | {clean_text(row['program_subcategory'])}"
            f" | {clean_text(row['program_expense_description'])} |"
        ),
    ])


def normalize_for_match(text: str) -> str:
    """
    Lower case, punctuation and bullets removed, single spaces.
    Accents are kept, so a paraphrase is still caught.
    """

    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def check_evidence(
    row: pd.Series,
    client_evidence,
    program_evidence,
    decision: str,
) -> str:
    """
    Empty string when both evidence strings appear in the texts
    (ignoring case and punctuation), otherwise the problem.
    """

    client_text = normalize_for_match(
        clean_text(row["client_expense_title"])
        + " "
        + clean_text(row["client_expense_description"])
    )

    program_text = normalize_for_match(" ".join([
        clean_text(row["program_category"]),
        clean_text(row["program_subcategory"]),
        clean_text(row["program_expense_description"]),
    ]))

    problems = []

    if not clean_text(client_evidence):
        if decision != "NO":
            problems.append("client_evidence missing")

    elif normalize_for_match(clean_text(client_evidence)) not in client_text:
        problems.append("client_evidence not verbatim")

    if normalize_for_match(clean_text(program_evidence)) not in program_text:
        problems.append("program_evidence not verbatim")

    return "; ".join(problems)


def to_label(decision: str, pair_category: str) -> str:
    """
    PLAUSIBLE confirms a pair built as positive, but marks a pair
    built as negative as ambiguous.
    """

    if decision != "PLAUSIBLE":
        return DECISION_TO_LABEL[decision]

    if "negative" in pair_category:
        return EXCLUDED

    return "Match"


def judge_pair(llm, system_prompt: str, row: pd.Series) -> Evaluation:

    response = llm.invoke(
        [
            ("system", system_prompt),
            ("human", build_user_message(row)),
        ]
    )

    data = json.loads(
        clean_json_response(str(response.content))
    )

    parsed = GuidelineResponse.model_validate(data)

    evaluations = [
        evaluation
        for result in parsed.results
        for evaluation in result.evaluations
    ]

    assert len(evaluations) == 1, f"Expected 1 evaluation, got {len(evaluations)}."

    return evaluations[0]


def judge_pair_with_retry(llm, system_prompt: str, row: pd.Series) -> Evaluation:

    for attempt in range(1, MAX_LLM_RETRIES + 1):

        try:
            return judge_pair(llm, system_prompt, row)

        except Exception as exc:

            print(f"  LLM error (attempt {attempt}/{MAX_LLM_RETRIES}): {exc}")

            if attempt == MAX_LLM_RETRIES:
                raise

            time.sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError("unreachable")


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Check the 100 or 300 review pairs with matching guideline v0.4."
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Judge at most this many new pairs (for a small test).",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the first user message and exit. No API call.",
    )

    parser.add_argument(
        "--sample",
        type=int,
        choices=[100, 300],
        default=100,
        help="Which review sample to check.",
    )

    parser.add_argument(
        "--run",
        default=None,
        help="Run name added to the output file names, e.g. run2.",
    )

    args = parser.parse_args()

    REVIEW_INPUT_PATH, REVIEW_COMPARISON_PATH = sample_paths(args.sample)

    CHECKPOINT_PATH, OUTPUT_PATH = run_paths(args.sample, args.run)

    input_df = pd.read_excel(REVIEW_INPUT_PATH)

    system_prompt = load_system_prompt()

    if args.dry_run:
        print(build_user_message(input_df.iloc[0]))
        print()
        print("System prompt characters:", len(system_prompt))
        return

    # --------------------------------------------------------
    # Resume from checkpoint
    # --------------------------------------------------------

    done_ids = set()

    if CHECKPOINT_PATH.exists():
        done_ids = set(
            pd.read_csv(CHECKPOINT_PATH, encoding="utf-8-sig")["review_id"]
        )

    todo_df = input_df[~input_df["review_id"].isin(done_ids)]

    if args.limit is not None:
        todo_df = todo_df.head(args.limit)

    print(f"Already done: {len(done_ids)}, to judge now: {len(todo_df)}")

    # --------------------------------------------------------
    # Judge
    # --------------------------------------------------------

    llm = create_llm(temperature=0)

    for number, (_, row) in enumerate(todo_df.iterrows(), start=1):

        print(f"[{number}/{len(todo_df)}] {row['review_id']}")

        evaluation = judge_pair_with_retry(llm, system_prompt, row)

        record = pd.DataFrame([{
            "review_id": row["review_id"],
            "v04_decision": evaluation.decision,
            "v04_confidence": evaluation.confidence,
            "v04_client_evidence": evaluation.client_evidence,
            "v04_program_evidence": evaluation.program_evidence,
            "v04_reason": evaluation.reason,
            "evidence_problem": check_evidence(
                row,
                evaluation.client_evidence,
                evaluation.program_evidence,
                evaluation.decision,
            ),
        }])

        record.to_csv(
            CHECKPOINT_PATH,
            mode="a",
            header=not CHECKPOINT_PATH.exists(),
            index=False,
            encoding="utf-8-sig",
        )

    # --------------------------------------------------------
    # Compare with reference labels
    # --------------------------------------------------------

    decisions_df = pd.read_csv(CHECKPOINT_PATH, encoding="utf-8-sig")

    assert decisions_df["review_id"].is_unique, "Duplicate review_id in checkpoint."

    comparison_df = pd.read_excel(REVIEW_COMPARISON_PATH)

    reference_df = comparison_df[[
        "review_id",
        "source_pair_id",
        "pair_category",
        "client_expense_title",
        "client_expense_description",
        "expense_class",
        "program_category",
        "program_subcategory",
        "program_expense_description",
        "our_label",
        "manual_review_label",
        "manual_review_comment",
    ]]

    # Reference: the manual label when filled, otherwise our label
    # where the blind reviewer agreed with it. A disagreement that
    # has not been judged yet is Pending and is not scored.
    reference_df["reference_label"] = (
        reference_df["manual_review_label"]
        .fillna(reference_df["our_label"].where(comparison_df["agreement"]))
        .fillna(PENDING)
    )

    reference_df["seen_before"] = comparison_df.get("seen_before", False)

    df = reference_df.merge(decisions_df, on="review_id", validate="one_to_one")

    # Recomputed, so older checkpoints use the current evidence check
    df["evidence_problem"] = [
        check_evidence(row, row["v04_client_evidence"], row["v04_program_evidence"], row["v04_decision"])
        for _, row in df.iterrows()
    ]

    df["v04_label"] = [
        to_label(decision, category)
        for decision, category in zip(df["v04_decision"], df["pair_category"])
    ]

    excluded = df["v04_label"] == EXCLUDED

    pending = df["reference_label"] == PENDING

    df["agreement"] = (df["v04_label"] == df["reference_label"]).astype(object)

    df.loc[excluded | pending, "agreement"] = None

    # Disagreements first, then excluded, then agreements
    df["sort_key"] = df["agreement"].map({False: 0, True: 2}).fillna(1)

    df = df.sort_values(["sort_key", "review_id"]).reset_index(drop=True)

    excluded = df["v04_label"] == EXCLUDED

    pending = df["reference_label"] == PENDING

    kept = df[~excluded & ~pending]

    hard = df[df["manual_review_label"].notna()]

    df[OUTPUT_COLUMNS].to_excel(
        OUTPUT_PATH,
        sheet_name="v04_decisions",
        index=False,
        engine="openpyxl",
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    per_category = pd.DataFrame({
        "pairs": df.groupby("pair_category").size(),
        "pending": df[pending].groupby("pair_category").size(),
        "kept": kept.groupby("pair_category").size(),
        "correct": kept.groupby("pair_category")["agreement"].sum(),
        "excluded": df[excluded].groupby("pair_category").size(),
    }).fillna(0).astype(int)

    correct = int(kept["agreement"].sum())

    hard_kept = hard[hard["v04_label"] != EXCLUDED]

    print("=" * 70)
    print("GUIDELINE v0.4 CHECK")
    print("=" * 70)
    print("Pairs judged:", len(df))
    print("Excluded:", int(excluded.sum()))
    print("Kept:", len(kept))
    print("Pending (disagreement not judged yet):", int(pending.sum()))
    print("Accuracy on kept:", correct, "/", len(kept), f"({correct / len(kept):.0%})")

    unseen = kept[~kept["seen_before"].astype(bool)]

    if len(unseen) < len(kept):
        print(
            "Accuracy on kept, without seen_before:",
            int(unseen["agreement"].sum()), "/", len(unseen),
            f"({unseen['agreement'].mean():.0%})",
        )
    print(
        "Former disagreements:",
        int(hard_kept["agreement"].sum()), "/", len(hard_kept), "correct,",
        len(hard) - len(hard_kept), "excluded",
    )
    print()
    print("Per category:")
    print(per_category.to_string())
    print()
    print("Decision x confidence:")
    print(pd.crosstab(df["v04_decision"], df["v04_confidence"]).to_string())
    print()
    print("Evidence problems:", int((df["evidence_problem"].fillna("") != "").sum()))
    print()
    print("Saved:", OUTPUT_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
