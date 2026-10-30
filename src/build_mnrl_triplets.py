import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import PROCESSED_DATA_DIR
from src.build_evaluation_set import text_key


# ============================================================
# Settings
#
# Build one MNRL triplet per training Match pair when a reviewed
# high-confidence NO_MATCH candidate is available. Use the lowest
# candidate rank. Stored training texts are preserved unchanged.
# Read-only inputs; no API calls or embedding computation.
# ============================================================

PAIRS_PATH = PROCESSED_DATA_DIR / "final_pairs_v05.csv"
CANDIDATES_PATH = (
    PROCESSED_DATA_DIR
    / "subprogram_negative_candidates_for_synthetic_mnrl_training.csv"
)
REVIEWS_PATH = PROCESSED_DATA_DIR / "subprogram_negative_reviews.csv"
OUTPUT_PATH = PROCESSED_DATA_DIR / "mnrl_triplets_training.csv"

PAIR_COLUMNS = [
    "pair_id", "split", "label", "source", "pair_category",
    "client_text", "program_text",
]
CANDIDATE_COLUMNS = [
    "pair_id", "candidate_id", "candidate_rank", "candidate_program_text",
    "candidate_program_expense_id", "candidate_score", "correct_score",
]
REVIEW_COLUMNS = ["candidate_id", "decision", "confidence"]
OUTPUT_COLUMNS = [
    "pair_id", "source", "pair_category", "anchor", "positive", "negative",
    "negative_candidate_id", "negative_rank", "negative_program_expense_id",
    "correct_score", "negative_score",
]


# ============================================================
# Inputs and validation
# ============================================================

def read_input(path, columns):
    frame = pd.read_csv(path, encoding="utf-8-sig", keep_default_na=False, dtype=str)
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")
    return frame[columns].copy()


def require_unique_ids(frame, column, path):
    frame[column] = frame[column].str.strip()
    assert frame[column].ne("").all(), f"{path.name}: empty {column}."
    assert frame[column].is_unique, f"{path.name}: duplicate {column}."


def integer_column(frame, column, path):
    values = pd.to_numeric(frame[column], errors="raise")
    assert np.isfinite(values).all() and values.mod(1).eq(0).all(), (
        f"{path.name}: {column} must contain finite integers."
    )
    return values.astype(int)


def load_inputs():
    pairs = read_input(PAIRS_PATH, PAIR_COLUMNS)
    candidates = read_input(CANDIDATES_PATH, CANDIDATE_COLUMNS)
    reviews = read_input(REVIEWS_PATH, REVIEW_COLUMNS)

    require_unique_ids(pairs, "pair_id", PAIRS_PATH)
    require_unique_ids(candidates, "candidate_id", CANDIDATES_PATH)
    require_unique_ids(reviews, "candidate_id", REVIEWS_PATH)
    candidates["pair_id"] = candidates["pair_id"].str.strip()
    assert candidates["pair_id"].isin(pairs["pair_id"]).all(), (
        "Candidates contain pair_id values absent from final_pairs_v05.csv."
    )
    pairs["label"] = integer_column(pairs, "label", PAIRS_PATH)
    assert pairs["label"].isin([0, 1]).all(), "Labels must be 0 or 1."
    assert pairs["split"].isin(["training", "validation", "test"]).all(), (
        "Unknown split in final_pairs_v05.csv."
    )
    training = pairs.loc[pairs["split"].eq("training") & pairs["label"].eq(1)].copy()
    for column in ["client_text", "program_text"]:
        assert training[column].str.strip().ne("").all(), f"Empty training {column}."

    candidates["candidate_rank"] = integer_column(candidates, "candidate_rank", CANDIDATES_PATH)
    assert candidates["candidate_rank"].isin([1, 2, 3]).all(), (
        "Candidate ranks must be 1, 2 or 3."
    )
    candidates["candidate_program_expense_id"] = integer_column(
        candidates, "candidate_program_expense_id", CANDIDATES_PATH,
    )
    assert candidates["candidate_program_expense_id"].gt(0).all()
    for column in ["candidate_score", "correct_score"]:
        candidates[column] = pd.to_numeric(candidates[column], errors="raise")
        assert np.isfinite(candidates[column]).all(), f"Non-finite {column}."
    for column in ["decision", "confidence"]:
        reviews[column] = reviews[column].str.strip()
    return pairs, training, candidates, reviews


# ============================================================
# Reviewed negative selection and triplets
# ============================================================

def build_triplets(pairs, training, candidates, reviews):
    # An inner join deliberately excludes candidates without a decision.
    reviewed = candidates.merge(reviews, on="candidate_id", how="inner", validate="one_to_one")
    eligible = reviewed.loc[
        reviewed["pair_id"].isin(training["pair_id"])
        & reviewed["decision"].eq("NO_MATCH")
        & reviewed["confidence"].eq("high")
    ]
    # Candidate ID provides a reproducible tie-break if ranks are equal.
    selected = eligible.sort_values(
        ["pair_id", "candidate_rank", "candidate_id"], kind="stable",
    ).drop_duplicates("pair_id", keep="first")
    joined = training.merge(selected, on="pair_id", how="inner", validate="one_to_one", sort=False)

    triplets = pd.DataFrame({
        "pair_id": joined["pair_id"],
        "source": joined["source"],
        "pair_category": joined["pair_category"],
        "anchor": joined["client_text"],
        "positive": joined["program_text"],
        "negative": joined["candidate_program_text"],
        "negative_candidate_id": joined["candidate_id"],
        "negative_rank": joined["candidate_rank"],
        "negative_program_expense_id": joined["candidate_program_expense_id"],
        "correct_score": joined["correct_score"],
        "negative_score": joined["candidate_score"],
    }, columns=OUTPUT_COLUMNS)

    assert triplets["pair_id"].isin(training["pair_id"]).all(), (
        "Every triplet must come from a training Match pair."
    )
    assert triplets["pair_id"].is_unique, "More than one triplet per pair_id."
    assert triplets["negative"].str.strip().ne("").all(), "Empty negative text."
    assert triplets["negative"].map(text_key).ne(triplets["positive"].map(text_key)).all(), (
        "A selected negative has the same normalized text as its positive."
    )
    held_out_clients = set(
        pairs.loc[pairs["split"].isin(["validation", "test"]), "client_text"].map(text_key)
    )
    assert set(triplets["anchor"].map(text_key)).isdisjoint(held_out_clients), (
        "Triplet anchors overlap with validation or test clients."
    )
    approved_ids = set(reviews.loc[
        reviews["decision"].eq("NO_MATCH") & reviews["confidence"].eq("high"), "candidate_id",
    ])
    assert triplets["negative_candidate_id"].isin(approved_ids).all(), (
        "Selected candidates must have high-confidence NO_MATCH decisions."
    )
    without_triplet = training.loc[~training["pair_id"].isin(triplets["pair_id"]), "pair_id"]
    assert len(triplets) + len(without_triplet) == len(training)
    return triplets, without_triplet


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Build training MNRL triplets from reviewed negatives.")
    parser.add_argument("--dry-run", action="store_true", help="Run selection and checks without writing files.")
    parser.add_argument("--overwrite", action="store_true", help="Replace the existing output CSV.")
    args = parser.parse_args()
    if OUTPUT_PATH.exists() and not args.overwrite and not args.dry_run:
        raise FileExistsError(f"Output exists: {OUTPUT_PATH}. Use --overwrite to replace it.")

    pairs, training, candidates, reviews = load_inputs()
    triplets, without_triplet = build_triplets(pairs, training, candidates, reviews)

    print("=" * 70)
    print("MNRL TRAINING TRIPLETS")
    print("=" * 70)
    print("Training Match pairs:", len(training))
    print("Triplets built:", len(triplets))
    print("Pairs without a high-confidence NO_MATCH candidate:", len(without_triplet))
    for pair_id in without_triplet:
        print(" ", pair_id)
    print()
    print("Triplets per negative rank:")
    print(triplets["negative_rank"].value_counts().reindex([1, 2, 3], fill_value=0).to_string())
    print()
    print("Triplets with negative_score > correct_score:", int(
        triplets["negative_score"].gt(triplets["correct_score"]).sum()
    ))
    print("All triplet assertions passed.")
    if args.dry_run:
        print("Dry run complete: no output file written.")
        return

    # Prepare the CSV before touching the destination; exclusive creation
    # also protects against another process creating it during selection.
    contents = triplets.to_csv(index=False).encode("utf-8-sig")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("wb" if args.overwrite else "xb") as handle:
        handle.write(contents)
    print("Saved:", OUTPUT_PATH)


if __name__ == "__main__":
    main()
