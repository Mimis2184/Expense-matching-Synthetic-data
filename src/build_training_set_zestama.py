import argparse
import io
import sys
from pathlib import Path

import pandas as pd


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
    PROCESSED_DATA_DIR,
)

from src.build_evaluation_set import (
    load_program_expenses,
)

from src.build_annotation_sample import (
    EASY_PATH,
    FIRST_EXCEL_ROW,
    NEW_SCORE_PATH,
    NON_LEXICAL_PATH,
    OUR_LABELS,
    POSITIVES_PATH,
    SEMI_HARD_PATH,
    clean_value,
    load_human_pairs,
    load_non_lexical_pairs,
    load_standard_pairs,
    pair_key,
    text_key,
)

from src.evaluate_embeddings_on_pairs import (
    agreement_is_true,
    build_texts,
)


# ============================================================
# POC training set
#
# Existing synthetic pairs for fine-tuning the embedding model.
# Reserved for evaluation: the complete 100-pair validation sample
# and the test pairs of the 300-pair sample (manual label, or our
# label where the blind review agreed) - the same rule as
# evaluate_embeddings_on_pairs.py.
#
# The remaining 300-sample disagreements go to training with the
# label of the blind review (guideline v0.4_100pairs):
# DIRECT / GENERIC / RESIDUAL -> Match, NO -> No match,
# PLAUSIBLE -> Match for positive / non-lexical pairs and left out
# for easy / semi-hard negatives.
#
# Exclude by source_pair_id and by the shared pair_key, then
# remove training duplicates. A client may still occur with a
# different program expense; this overlap is reported only.
#
# Text formatting is imported from the baseline evaluation.
# No model is loaded, no embeddings are computed, no API call.
# Input files are not modified.
# ============================================================

VALIDATION_KEY_PATH = OUTPUT_DATA_DIR / "review_100_pairs_our_labels.xlsx"
TEST_COMPARISON_PATH = OUTPUT_DATA_DIR / "review_300_pairs_comparison.xlsx"
CLAUDE_DECISIONS_PATH = OUTPUT_DATA_DIR / "review_300_pairs_claude_decisions.csv"

CSV_OUTPUT_PATH = PROCESSED_DATA_DIR / "training_pairs_poc.csv"
EXCEL_OUTPUT_PATH = PROCESSED_DATA_DIR / "training_pairs_poc.xlsx"

LABELS = {"Match": 1, "No match": 0}

DECISION_TO_LABEL = {
    "DIRECT": "Match",
    "GENERIC": "Match",
    "RESIDUAL": "Match",
    "NO": "No match",
}

OUTPUT_COLUMNS = [
    "source_pair_id",
    "pair_category",
    "client_text",
    "program_text",
    "label",
    "label_text",
    "label_source",
    "client_expense_title",
    "client_expense_description",
    "program_category",
    "program_subcategory",
    "program_expense_description",
    "expense_class",
]


# ============================================================
# Reserved validation / test pairs
# ============================================================

def load_reserved_ids(path: Path) -> set[str]:

    df = pd.read_excel(
        path,
        sheet_name="review_key",
    )

    if "source_pair_id" not in df.columns:
        raise ValueError(f"{path}: missing source_pair_id column.")

    ids = df["source_pair_id"].map(clean_value)

    assert ids.ne("").all(), f"{path}: empty source_pair_id."

    assert ids.is_unique, f"{path}: duplicate source_pair_id."

    return set(ids)


def load_test_split():
    """
    Split the 300-pair sample into test pairs (reserved) and blind
    review disagreements (training, labelled by the review).
    Returns: test source_pair_ids, {source_pair_id: label} for the
    disagreements used in training, and the disagreement ids left
    out (PLAUSIBLE on a negative).
    """

    comparison = pd.read_excel(
        TEST_COMPARISON_PATH,
        sheet_name="review_comparison",
    )

    decisions = pd.read_csv(
        CLAUDE_DECISIONS_PATH,
        encoding="utf-8-sig",
    )

    for column in ["review_id", "source_pair_id", "pair_category"]:
        comparison[column] = comparison[column].map(clean_value)

    manual = comparison["manual_review_label"].map(clean_value)

    in_test = manual.ne("") | comparison["agreement"].map(agreement_is_true)

    test_ids = set(comparison.loc[in_test, "source_pair_id"])

    # The comparison file carries the review decision; check it
    # against the original decisions file before using it.
    check = comparison[["review_id", "claude_decision"]].merge(
        decisions[["review_id", "claude_decision"]],
        on="review_id",
        how="left",
        suffixes=("", "_original"),
        validate="one_to_one",
    )

    assert check["claude_decision"].eq(check["claude_decision_original"]).all(), (
        "Review decisions differ between the comparison and the decisions file."
    )

    disagreements = comparison.loc[~in_test]

    assert disagreements["claude_decision"].notna().all(), (
        "A disagreement has no blind review decision."
    )

    claude_labels = {}

    left_out = set()

    for _, row in disagreements.iterrows():

        decision = row["claude_decision"]

        if decision == "PLAUSIBLE":
            if "negative" in row["pair_category"]:
                left_out.add(row["source_pair_id"])
            else:
                claude_labels[row["source_pair_id"]] = "Match"
            continue

        assert decision in DECISION_TO_LABEL, f"Unknown decision: {decision}"

        claude_labels[row["source_pair_id"]] = DECISION_TO_LABEL[decision]

    return test_ids, claude_labels, left_out


def reserved_pair_keys(all_pairs, source_ids, path):
    """
    Recover program IDs and pair keys through the shared loaders.
    Never silently ignore reserved IDs missing from current sources.
    """

    missing = source_ids - set(all_pairs["source_pair_id"])

    if missing:
        raise ValueError(
            f"{path.name}: {len(missing)} reserved source_pair_id values "
            "cannot be resolved in the current synthetic pairs. "
            "Check that the source files have not changed. "
            f"Examples: {sorted(missing)[:5]}"
        )

    return set(
        all_pairs.loc[
            all_pairs["source_pair_id"].isin(source_ids),
            "_pair_key",
        ]
    )


# ============================================================
# Non-lexical pairs of programs without a Mapping file
# ============================================================

def recover_non_lexical_by_label(program_df, linked_ids):
    """
    The shared loader drops non-lexical pilot pairs whose example
    program (e.g. Ταμείο Ανάκαμψης Θεσσαλίας) has no Mapping file.
    Recover them for training only: link to a program expense of
    another program with the same category and subcategory and an
    empty description, so the program text is exactly the same.
    The reserved validation/test samples are not affected.
    """

    pilot = pd.read_excel(
        NON_LEXICAL_PATH,
        sheet_name="pairs",
    ).reset_index(drop=True)

    pilot["_source_file_row"] = pilot.index + FIRST_EXCEL_ROW

    pilot = pilot[pilot["new_score"] == 0]

    scores = pd.read_excel(NEW_SCORE_PATH)

    human = load_human_pairs()

    no_description = program_df["expense_description"].map(clean_value).eq("")

    rows = []

    not_found = 0

    for _, row in pilot.iterrows():

        source_pair_id = (
            f"{NON_LEXICAL_PATH.name} · "
            f"row={int(row['_source_file_row'])} · "
            f"source_row={int(row['source_row'])}"
        )

        if source_pair_id in linked_ids:
            continue

        example = scores.iloc[int(row["source_row"]) - FIRST_EXCEL_ROW]

        human_match = human[
            (human["_Business_Expense_key"] == text_key(example["Client Expense"]))
            & (human["_Business_Expense_description_key"] == text_key(example["Client Expense Description"]))
            & (human["_Category_key"] == text_key(example["Ideal Expense Category"]))
            & (human["_Subcategory_key"] == text_key(example["Ideal Expense Subcategory"]))
        ]

        if human_match.empty:
            not_found += 1
            continue

        human_row = human_match.iloc[0]

        program_match = program_df[
            (program_df["_category_key"] == text_key(human_row["Category"]))
            & (program_df["_subcategory_key"] == text_key(human_row["Subcategory"]))
            & no_description
        ]

        if program_match.empty:
            not_found += 1
            continue

        # Same category, subcategory and empty description: the program
        # text is identical, so the choice among them does not matter.
        program = program_match.sort_values("program_expense_id").iloc[0]

        rows.append(
            {
                "category": "non_lexical_positive",
                "source_pair_id": source_pair_id,
                "client_expense_title": clean_value(row["client_expense_title"]),
                "client_expense_description": clean_value(row["client_expense_description"]),
                "program_expense_id": int(program["program_expense_id"]),
                "program_category": clean_value(program["category"]),
                "program_subcategory": clean_value(program["subcategory"]),
                "program_expense_description": clean_value(program["expense_description"]),
                "expense_class": clean_value(program["expense_class"]),
            }
        )

    return pd.DataFrame(rows), not_found


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Build the synthetic POC training set without validation/test pairs."
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing the existing training CSV and Excel files.",
    )

    args = parser.parse_args()

    for path in [CSV_OUTPUT_PATH, EXCEL_OUTPUT_PATH]:
        if path.exists() and not args.overwrite:
            raise FileExistsError(
                f"{path} already exists. Use --overwrite to replace it."
            )

    program_df = load_program_expenses()

    non_lexical_df, non_lexical_excluded = load_non_lexical_pairs(
        program_df
    )

    recovered_df, not_recovered = recover_non_lexical_by_label(
        program_df,
        set(non_lexical_df["source_pair_id"]),
    )

    non_lexical_df = pd.concat(
        [non_lexical_df, recovered_df],
        ignore_index=True,
    )

    sources = {
        "positive": load_standard_pairs(POSITIVES_PATH, "positive"),
        "easy_negative": load_standard_pairs(EASY_PATH, "easy_negative"),
        "semi_hard_negative": load_standard_pairs(SEMI_HARD_PATH, "semi_hard_negative"),
        "non_lexical_positive": non_lexical_df,
    }

    all_pairs = pd.concat(
        [df for df in sources.values() if not df.empty],
        ignore_index=True,
    ).rename(columns={"category": "pair_category"})

    if all_pairs.empty:
        raise ValueError("No synthetic pairs were loaded.")

    all_pairs["source_pair_id"] = all_pairs["source_pair_id"].map(clean_value)

    assert all_pairs["source_pair_id"].ne("").all()

    all_pairs["_pair_key"] = all_pairs.apply(pair_key, axis=1)

    all_pairs["_client_key"] = all_pairs["_pair_key"].map(lambda key: key[:2])

    # An ID must identify the same pair, even if it occurs more than once.
    ambiguous_ids = all_pairs.groupby("source_pair_id")["_pair_key"].nunique()

    if (ambiguous_ids > 1).any():
        raise ValueError("A source_pair_id refers to multiple different pair keys.")

    validation_ids = load_reserved_ids(VALIDATION_KEY_PATH)

    test_ids, claude_labels, claude_left_out = load_test_split()

    validation_keys = reserved_pair_keys(all_pairs, validation_ids, VALIDATION_KEY_PATH)

    test_keys = reserved_pair_keys(all_pairs, test_ids, TEST_COMPARISON_PATH)

    # The disagreements must exist in the current sources too.
    reserved_pair_keys(all_pairs, set(claude_labels) | claude_left_out, TEST_COMPARISON_PATH)

    reserved_ids = validation_ids | test_ids

    reserved_keys = validation_keys | test_keys

    # --------------------------------------------------------
    # Exclude validation/test, then remove training duplicates
    # --------------------------------------------------------

    removed_by_id = all_pairs["source_pair_id"].isin(reserved_ids)

    remaining = all_pairs.loc[~removed_by_id].copy()

    # This also catches a reserved pair stored under another source ID.
    removed_by_key = remaining["_pair_key"].isin(reserved_keys)

    remaining_after_keys = remaining.loc[~removed_by_key].copy()

    # Blind review: PLAUSIBLE on a negative is ambiguous, left out.
    removed_plausible = remaining_after_keys["source_pair_id"].isin(claude_left_out)

    remaining_after_keys = remaining_after_keys.loc[~removed_plausible].copy()

    duplicate_mask = remaining_after_keys["_pair_key"].duplicated()

    # Keep the first occurrence, in the same category order as the
    # annotation sampler (positive, easy, semi-hard, non-lexical).
    training_df = remaining_after_keys.loc[~duplicate_mask].copy().reset_index(drop=True)

    if training_df.empty:
        raise ValueError("No training pairs remain after exclusions.")

    from_review = training_df["source_pair_id"].isin(claude_labels)

    training_df["label_text"] = training_df["pair_category"].map(OUR_LABELS)

    training_df.loc[from_review, "label_text"] = (
        training_df.loc[from_review, "source_pair_id"].map(claude_labels)
    )

    training_df["label_source"] = "our_label"

    training_df.loc[from_review, "label_source"] = "claude_review"

    training_df["label"] = training_df["label_text"].map(LABELS)

    # Shared loaders already clean text values; normalize empty fields
    # explicitly before passing them to the baseline's text formatter.
    for column in [
        "client_expense_title", "client_expense_description",
        "program_category", "program_subcategory", "program_expense_description",
    ]:
        training_df[column] = training_df[column].map(clean_value)

    client_texts, program_texts = build_texts(training_df)

    training_df["client_text"] = client_texts

    training_df["program_text"] = program_texts

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    assert not training_df["source_pair_id"].isin(reserved_ids).any()

    assert not training_df["_pair_key"].isin(reserved_keys).any()

    assert training_df["source_pair_id"].is_unique

    assert training_df["_pair_key"].is_unique

    assert training_df["label_text"].isin(LABELS).all()

    assert training_df["label"].isin([0, 1]).all()

    assert set(training_df["label"]) == {0, 1}, "Training must contain both labels."

    for column in ["client_text", "program_text"]:
        assert training_df[column].notna().all()
        assert training_df[column].str.strip().ne("").all()

    # Formatting prefixes alone must not conceal an empty source text.
    assert (
        training_df["client_expense_title"].ne("")
        | training_df["client_expense_description"].ne("")
    ).all(), "A training client has no title or description."

    assert (
        training_df["program_category"].ne("")
        | training_df["program_subcategory"].ne("")
        | training_df["program_expense_description"].ne("")
    ).all(), "A training program expense has no text."

    assert (
        len(training_df) + int(removed_by_id.sum())
        + int(removed_by_key.sum()) + int(removed_plausible.sum())
        + int(duplicate_mask.sum())
        == len(all_pairs)
    )

    training_df["label"] = training_df["label"].astype(int)

    # --------------------------------------------------------
    # Client overlap (information only; these pairs are kept)
    # --------------------------------------------------------

    training_clients = set(training_df["_client_key"])

    validation_clients = {key[:2] for key in validation_keys}

    test_clients = {key[:2] for key in test_keys}

    # Since identical pair keys were excluded, a shared client here
    # necessarily has a different program expense in the reserved set.
    validation_overlap = training_clients & validation_clients

    test_overlap = training_clients & test_clients

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    output_df = training_df[OUTPUT_COLUMNS]

    # Prepare both exports before writing either output file.
    csv_bytes = output_df.to_csv(index=False).encode("utf-8-sig")

    excel_buffer = io.BytesIO()

    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        output_df.to_excel(
            writer,
            sheet_name="training_pairs",
            index=False,
        )

        sheet = writer.sheets["training_pairs"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

    PROCESSED_DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Recheck both destinations immediately before saving.
    for path in [CSV_OUTPUT_PATH, EXCEL_OUTPUT_PATH]:
        if path.exists() and not args.overwrite:
            raise FileExistsError(
                f"{path} already exists. Use --overwrite to replace it."
            )

    mode = "wb" if args.overwrite else "xb"

    with CSV_OUTPUT_PATH.open(mode) as handle:
        handle.write(csv_bytes)

    with EXCEL_OUTPUT_PATH.open(mode) as handle:
        handle.write(excel_buffer.getvalue())

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print("=" * 70)
    print("POC TRAINING SET")
    print("=" * 70)
    print("Synthetic pairs loaded:", len(all_pairs))
    print()
    print("Loaded per category:")
    for category, df in sources.items():
        print(f"  {category}: {len(df)}")
    print()
    print("Removed by validation/test source_pair_id:", int(removed_by_id.sum()))
    print("Removed by validation/test pair_key (additional):", int(removed_by_key.sum()))
    print("Removed: blind review PLAUSIBLE on a negative:", int(removed_plausible.sum()))
    print("Removed training duplicates:", int(duplicate_mask.sum()))
    print("Total removed after loading:", len(all_pairs) - len(training_df))
    print("Non-lexical pairs excluded by loader (no unique program expense):", non_lexical_excluded)
    print("  of which recovered by same category/subcategory, empty description:", len(recovered_df))
    print("  still not linked:", not_recovered)
    print()
    print("Training pairs:", len(training_df))
    print()
    # pair_category is how the pair was built, not its label: a pair
    # built as a negative can carry Match after the blind review.
    print("Training label per pair origin (pair_category = how the pair was built):")
    origin_table = pd.crosstab(
        training_df["pair_category"],
        training_df["label_text"],
        margins=True,
        margins_name="Total",
    ).reindex(list(sources) + ["Total"], fill_value=0)
    print(
        origin_table.reindex(columns=["Match", "No match", "Total"], fill_value=0)
        .to_string()
    )
    print()
    print("Disagreements of the 300 sample used in training:", int(from_review.sum()))
    print(
        training_df[from_review].groupby(["pair_category", "label_text"]).size().to_string()
    )
    print()
    print("Per label source:")
    print(training_df["label_source"].value_counts().to_string())
    print()
    print("Per label (Match = 1, No match = 0):")
    print(training_df.groupby(["label", "label_text"]).size().to_string())
    print()
    print("Unique training clients:", len(training_clients))
    print("Clients shared with validation, different program expense:", len(validation_overlap))
    print("Clients shared with test, different program expense:", len(test_overlap))
    print("Clients shared with validation or test (unique union):", len(validation_overlap | test_overlap))
    print("These shared clients were kept; no complete pair overlaps remain.")
    print()
    print("Saved:", CSV_OUTPUT_PATH)
    print("Saved:", EXCEL_OUTPUT_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
