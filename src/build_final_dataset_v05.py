import argparse
import io
import json
import math
import random
import sys
from pathlib import Path

import pandas as pd


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(BASE_DIR))

from config import CLASS_RENAMES, OUTPUT_DATA_DIR, PROCESSED_DATA_DIR
from src.build_training_set_zestama import (
    TEST_COMPARISON_PATH,
    load_test_split,
)
from src.evaluate_embeddings_on_pairs import (
    VALIDATION_PATH,
    agreement_is_true,
    build_texts,
    clean_value,
    load_pairs,
)


# ============================================================
# Final POC dataset (v0.5)
#
# Join reviewed v0.5 pairs and the existing review samples.
# Labels come from decisions / reviewed reference labels only.
# A pair's category records its origin, not necessarily its label.
#
# Fixed splits: review300_test -> test, review100 -> validation,
# review300_claude -> training (excluding PLAUSIBLE negatives).
# New pairs are grouped by anchor text or non-lexical source row.
# Shared clients connect groups and must stay in the same split.
# Default stratified mode excludes flag_review=True pairs before
# deduplication, then redistributes all components across 5 folds.
#
# Remove new clients overlapping test. Conflicting constraints
# in fixed old samples, or conflicting labels affecting the fixed
# test, stop the build rather than silently changing the test.
#
# No API call. No input file is modified. Dry run does not write.
# ============================================================

POSITIVE_PATH = OUTPUT_DATA_DIR / "positive_pairs_v05_uncovered.csv"
POSITIVE_DECISIONS_PATH = OUTPUT_DATA_DIR / "positive_pairs_v05_uncovered_claude_decisions.csv"
NEGATIVE_PATH = OUTPUT_DATA_DIR / "negative_candidates_v05.csv"
NEGATIVE_DECISIONS_PATH = OUTPUT_DATA_DIR / "negative_candidates_v05_claude_decisions.csv"
NON_LEXICAL_PATH = OUTPUT_DATA_DIR / "non_lexical_pairs_v05.csv"
NON_LEXICAL_DECISIONS_PATH = OUTPUT_DATA_DIR / "non_lexical_pairs_v05_claude_decisions.csv"

CSV_OUTPUT_PATH = PROCESSED_DATA_DIR / "final_pairs_v05.csv"
EXCEL_OUTPUT_PATH = PROCESSED_DATA_DIR / "final_pairs_v05.xlsx"

TEXT_COLUMNS = [
    "client_expense_title", "client_expense_description", "program_category",
    "program_subcategory", "program_expense_description",
]
REVIEW_COLUMNS = ["label", "decision", "confidence", "rule", "flag_review"]
CATEGORIES = {"positive", "easy_negative", "semi_hard_negative", "non_lexical_positive"}
LABEL_TEXT = {1: "Match", 0: "No match"}

OUTPUT_COLUMNS = [
    "pair_id", "split", "source", "pair_category", "group_key",
    "client_text", "program_text", "label", "label_text",
    "decision", "confidence", "rule", "flag_review", "program_expense_id",
    "client_expense_title", "client_expense_description", "program_category",
    "program_subcategory", "program_expense_description",
]


# ============================================================
# Input helpers
# ============================================================

def require_columns(df, columns, path):

    missing = sorted(set(columns) - set(df.columns))
    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")


def normalize_text(value):
    """Casefold, strip and collapse whitespace; do not remove accents."""

    return " ".join(clean_value(value).casefold().split())


def integer_values(values, name):

    numbers = pd.to_numeric(values, errors="raise")
    if numbers.isna().any() or (numbers % 1 != 0).any():
        raise ValueError(f"{name}: integer values are required.")
    return numbers.astype(int)


def read_csv(path):

    return pd.read_csv(path, encoding="utf-8-sig", keep_default_na=False).reset_index(drop=True)


def join_decisions(pairs, decisions, key, path, subset=False):
    """
    Join only decision fields; never accidentally use a proposed label
    or let duplicated text columns shadow the source pair's text.
    """

    require_columns(decisions, [key] + REVIEW_COLUMNS, path)
    assert pairs[key].is_unique, f"Duplicate source key: {key}."
    assert decisions[key].is_unique, f"{path.name}: duplicate {key}."
    assert decisions[key].isin(pairs[key]).all(), f"{path.name}: unknown {key}."

    if not subset:
        assert set(pairs[key]) == set(decisions[key]), f"{path.name}: incomplete decisions."

    # Check fields repeated in decision exports to catch stale / shifted IDs.
    shared = [
        column for column in ["program_expense_id", "source_row", "pair_category"] + TEXT_COLUMNS[:4]
        if column in decisions.columns and column in pairs.columns and column != key
    ]
    if shared:
        check = pairs[[key] + shared].merge(
            decisions[[key] + shared], on=key, how="inner",
            suffixes=("_pair", "_decision"), validate="one_to_one",
        )
        for column in shared:
            if column in {"program_expense_id", "source_row"}:
                left = integer_values(check[f"{column}_pair"], column)
                right = integer_values(check[f"{column}_decision"], column)
            else:
                left = check[f"{column}_pair"].map(clean_value)
                right = check[f"{column}_decision"].map(clean_value)
            assert left.eq(right).all(), f"{path.name}: source and decisions differ on {column}."

    pairs = pairs.drop(columns=[column for column in REVIEW_COLUMNS if column in pairs.columns])
    return pairs.merge(decisions[[key] + REVIEW_COLUMNS], on=key, how="inner", validate="one_to_one")


def prepare_frame(df, source, origin_column, fixed_split=""):

    df = df.copy()
    require_columns(df, TEXT_COLUMNS + ["label", "pair_category"], Path(source))

    for column in TEXT_COLUMNS:
        df[column] = df[column].map(clean_value)

    df["label"] = integer_values(df["label"], f"{source}: label")
    assert df["label"].isin([0, 1]).all(), f"{source}: invalid label."
    assert df["pair_category"].isin(CATEGORIES).all(), f"{source}: unknown pair_category."

    for column in ["decision", "confidence", "rule", "program_expense_id"]:
        if column not in df:
            df[column] = ""
        df[column] = df[column].map(clean_value)

    if "flag_review" not in df:
        df["flag_review"] = False
    df["flag_review"] = df["flag_review"].map(agreement_is_true)
    df["source"] = source
    df["_origin_id"] = source + ":" + df[origin_column].astype(str)
    df["_fixed_split"] = fixed_split
    df["_is_new"] = not bool(fixed_split)
    df["label_text"] = df["label"].map(LABEL_TEXT)
    df["client_text"], df["program_text"] = build_texts(df)
    df["_client_key"] = df["client_text"].map(normalize_text)
    return df


def anchor_group(row):

    return "v05:" + json.dumps([
        clean_value(row[column])
        for column in ["program_category", "program_subcategory", "program_expense_description"]
    ], ensure_ascii=False)


def load_new_pairs():

    positives = read_csv(POSITIVE_PATH)
    require_columns(positives, TEXT_COLUMNS + ["program_expense_id"], POSITIVE_PATH)
    positives["row"] = positives.index + 1
    positive_decisions = read_csv(POSITIVE_DECISIONS_PATH)
    positive_decisions["row"] = integer_values(positive_decisions["row"], "positive row")

    # Keep the complete original anchor lookup, independently of its label.
    positives["group_key"] = positives.apply(anchor_group, axis=1)
    anchors = positives.set_index("row")
    positive_pairs = join_decisions(positives, positive_decisions, "row", POSITIVE_DECISIONS_PATH)
    positive_pairs["pair_category"] = "positive"
    positive_pairs = prepare_frame(positive_pairs, "positives_v05", "row")

    negatives = read_csv(NEGATIVE_PATH)
    require_columns(negatives, TEXT_COLUMNS + [
        "candidate_id", "pair_category", "source_row", "anchor_program_expense_id",
    ], NEGATIVE_PATH)
    negative_decisions = read_csv(NEGATIVE_DECISIONS_PATH)
    require_columns(negative_decisions, ["candidate_id", "use_in_training"], NEGATIVE_DECISIONS_PATH)
    negative_pairs = join_decisions(negatives, negative_decisions, "candidate_id", NEGATIVE_DECISIONS_PATH)
    keep_ids = set(negative_decisions.loc[
        negative_decisions["use_in_training"].map(agreement_is_true), "candidate_id",
    ])
    negative_pairs = negative_pairs.loc[negative_pairs["candidate_id"].isin(keep_ids)].copy()
    print("Negative candidates excluded by use_in_training:", len(negatives) - len(negative_pairs))

    negative_pairs["source_row"] = integer_values(negative_pairs["source_row"], "negative source_row")
    assert negative_pairs["source_row"].isin(anchors.index).all(), "Negative has unknown positive source_row."
    anchor_ids = negative_pairs["source_row"].map(anchors["program_expense_id"])
    assert integer_values(negative_pairs["anchor_program_expense_id"], "anchor ID").eq(
        integer_values(anchor_ids, "positive anchor ID")
    ).all(), "Negative anchor differs from its positive source row."
    for column in ["client_expense_title", "client_expense_description"]:
        assert negative_pairs[column].map(normalize_text).eq(
            negative_pairs["source_row"].map(anchors[column]).map(normalize_text)
        ).all(), "Negative client differs from its positive source row."
    negative_pairs["group_key"] = negative_pairs["source_row"].map(anchors["group_key"])
    assert negative_pairs["pair_category"].isin(["easy_negative", "semi_hard_negative"]).all()
    negative_pairs = prepare_frame(negative_pairs, "negatives_v05", "candidate_id")

    non_lexical = read_csv(NON_LEXICAL_PATH)
    require_columns(non_lexical, TEXT_COLUMNS + ["source_row", "new_score"], NON_LEXICAL_PATH)
    non_lexical["nl_id"] = [f"L{number}" for number in range(1, len(non_lexical) + 1)]
    nl_decisions = read_csv(NON_LEXICAL_DECISIONS_PATH)
    nl_pairs = join_decisions(non_lexical, nl_decisions, "nl_id", NON_LEXICAL_DECISIONS_PATH, subset=True)
    assert pd.to_numeric(nl_pairs["new_score"], errors="raise").eq(0).all(), (
        "A reviewed non-lexical pair has nonzero new_score."
    )
    nl_pairs["source_row"] = integer_values(nl_pairs["source_row"], "non-lexical source_row")
    assert nl_pairs["source_row"].gt(0).all()
    nl_pairs["group_key"] = "nl:" + nl_pairs["source_row"].astype(str)
    nl_pairs["pair_category"] = "non_lexical_positive"
    nl_pairs = prepare_frame(nl_pairs, "non_lexical_v05", "nl_id")
    print("Non-lexical rows without decisions:", len(non_lexical) - len(nl_pairs))

    return pd.concat([positive_pairs, negative_pairs, nl_pairs], ignore_index=True)


# ============================================================
# Old reviewed samples and class renames
# ============================================================

def rename_old_fields(df):

    df = df.copy()
    changed = pd.Series(False, index=df.index)
    for column in TEXT_COLUMNS + ["expense_class", "client_expense_class", "program_expense_class"]:
        if column not in df:
            continue
        original = df[column].map(clean_value)
        renamed = original.copy()
        for old, new in CLASS_RENAMES.items():
            renamed = renamed.map(lambda text: text.replace(old, new))
        changed |= original.ne(renamed)
        df[column] = renamed
    return df, int(changed.sum())


def load_old_pairs(split_mode="fixed"):

    test_ids, claude_labels, left_out = load_test_split()
    test, _, _ = load_pairs(TEST_COMPARISON_PATH, validation=False)
    if split_mode == "fixed":
        validation, _, _ = load_pairs(VALIDATION_PATH, validation=True)
    else:
        # Preserve the old reference-label rule without imposing a
        # fixed 100-pair split in the new stratified build.
        validation = pd.read_excel(VALIDATION_PATH, sheet_name="review_comparison")
        require_columns(validation, TEXT_COLUMNS + [
            "review_id", "source_pair_id", "pair_category", "manual_review_label", "our_label",
        ], VALIDATION_PATH)
        manual = validation["manual_review_label"].map(clean_value)
        original = validation["our_label"].map(clean_value)
        assert (manual.eq("") | manual.isin(["Match", "No match"])).all()
        assert original.isin(["Match", "No match"]).all()
        validation["label"] = manual.where(manual.ne(""), original).map({"Match": 1, "No match": 0})
        assert validation["review_id"].is_unique
        assert validation["source_pair_id"].is_unique
    comparison = pd.read_excel(TEST_COMPARISON_PATH, sheet_name="review_comparison")
    comparison["source_pair_id"] = comparison["source_pair_id"].map(clean_value)
    assert set(test["source_pair_id"]) == test_ids
    if split_mode == "fixed":
        assert len(test) == 264, f"Expected the fixed 264-pair test; loader now returns {len(test)}."

    claude = comparison.loc[comparison["source_pair_id"].isin(claude_labels)].copy()
    assert len(claude) == len(claude_labels)
    claude["label"] = claude["source_pair_id"].map(claude_labels).map({"Match": 1, "No match": 0})
    print("Review300 disagreements used:", len(claude), "| PLAUSIBLE negatives excluded:", len(left_out))

    frames = []
    renamed_count = 0
    for frame, source, split in [
        (test, "review300_test", "test"),
        (validation, "review100", "validation"),
        (claude, "review300_claude", "training"),
    ]:
        frame, count = rename_old_fields(frame)
        renamed_count += count
        frame["decision"] = ""
        frame["confidence"] = ""
        frame["rule"] = ""
        frame["flag_review"] = False
        frame = prepare_frame(
            frame, source, "review_id",
            fixed_split=split if split_mode == "fixed" else "",
        )
        # Source provenance is independent of its new split assignment.
        frame["_is_new"] = False
        frame["group_key"] = "old:" + frame["_client_key"]
        frames.append(frame)

    print("Old pairs changed by CLASS_RENAMES:", renamed_count)
    return pd.concat(frames, ignore_index=True)


# ============================================================
# Duplicate and leakage checks
# ============================================================

def report_rows(df, title):

    if not df.empty:
        print(title, f"({len(df)} pairs)")
        print(df[["_origin_id", "source", "label", "client_text", "program_text"]].to_string(index=False))


def remove_overlaps_and_duplicates(df):

    counts = {}
    # Diagnose contradictory labels on reserved pairs before the leakage
    # filter could hide a conflicting new duplicate of a fixed old pair.
    original_conflicts = df.groupby(
        ["client_text", "program_text"], sort=False,
    )["label"].transform("nunique").gt(1)
    if (original_conflicts & df["source"].isin(["review300_test", "review100"])).any():
        report_rows(df.loc[original_conflicts], "Conflicting labels involving reserved samples:")
        raise ValueError(
            "Removing all conflicting duplicates would change the fixed test or validation. "
            "Resolve the reference labels first; no files written."
        )
    test_clients = set(df.loc[df["source"].eq("review300_test"), "_client_key"])
    # Everything except the fixed test drops pairs whose client is in test.
    leaking = df["source"].ne("review300_test") & df["_client_key"].isin(test_clients)
    report_rows(df.loc[leaking], "Removed pairs sharing a client with test:")
    counts["test_client_leakage"] = int(leaking.sum())
    removed_review100 = int((leaking & df["source"].eq("review100")).sum())
    print("review100 pairs removed for test overlap:", removed_review100)
    df = df.loc[~leaking].copy()

    # review300_claude is fixed to training, so it cannot follow a shared
    # client into validation; drop it instead. New pairs are moved later.
    val_clients = set(df.loc[df["source"].eq("review100"), "_client_key"])
    val_leaking = df["source"].eq("review300_claude") & df["_client_key"].isin(val_clients)
    report_rows(df.loc[val_leaking], "Removed review300_claude pairs sharing a client with review100:")
    counts["validation_client_leakage"] = int(val_leaking.sum())
    df = df.loc[~val_leaking].copy()

    # Conflicting labels: remove every occurrence, not just the first two.
    # Any resulting change to the fixed test is an incompatible request.
    conflicts = df.groupby(["client_text", "program_text"], sort=False)["label"].transform("nunique").gt(1)
    report_rows(df.loc[conflicts], "Conflicting duplicate labels (all occurrences excluded):")
    if (conflicts & df["source"].eq("review300_test")).any():
        raise ValueError("Conflicting duplicate labels affect the fixed test. Resolve them before rebuilding; no files written.")
    if (conflicts & df["source"].eq("review100")).any():
        raise ValueError("Conflicting duplicate labels affect the fixed 100 validation pairs. Resolve them first; no files written.")
    counts["conflicting_duplicate_rows"] = int(conflicts.sum())
    df = df.loc[~conflicts].copy()

    # Prefer the fixed test, then old validation, then old training.
    # This avoids replacing a reserved pair with a new duplicate.
    priority = {"review300_test": 0, "review100": 1, "review300_claude": 2}
    df["_priority"] = df["source"].map(priority).fillna(3)
    df = df.sort_values("_priority", kind="stable")
    duplicates = df.duplicated(["client_text", "program_text"], keep="first")
    report_rows(df.loc[duplicates], "Removed same-label duplicate pairs:")
    counts["same_label_duplicate_rows"] = int(duplicates.sum())
    df = df.loc[~duplicates].copy().reset_index(drop=True)

    assert len(df.loc[df["source"].eq("review300_test")]) == 264, (
        "Deduplication changes the fixed test size; inspect duplicate test pairs."
    )
    assert len(df.loc[df["source"].eq("review100")]) == 100 - removed_review100, (
        "Deduplication changes the fixed validation sample; inspect duplicates."
    )
    return df, counts


# ============================================================
# Connected groups and split assignment
# ============================================================

def remove_stratified_duplicates(df):
    """
    Same duplicate rules as fixed mode, without protected samples
    or client-overlap removals. Conflicting labels drop all copies.
    """

    df = df.copy()
    low_confidence = df["flag_review"]
    report_rows(df.loc[low_confidence], "Removed flag_review=True pairs before deduplication:")
    low_confidence_count = int(low_confidence.sum())
    df = df.loc[~low_confidence].copy()
    assert not df["flag_review"].any()

    conflicts = df.groupby(
        ["client_text", "program_text"], sort=False,
    )["label"].transform("nunique").gt(1)
    report_rows(df.loc[conflicts], "Conflicting duplicate labels (all occurrences excluded):")
    counts = {
        "test_client_leakage": 0,
        "validation_client_leakage": 0,
        "low_confidence_excluded": low_confidence_count,
        "conflicting_duplicate_rows": int(conflicts.sum()),
    }
    df = df.loc[~conflicts].copy()

    # Keep the same source preference as the existing fixed build.
    priority = {"review300_test": 0, "review100": 1, "review300_claude": 2}
    df["_priority"] = df["source"].map(priority).fillna(3)
    df = df.sort_values("_priority", kind="stable")
    duplicates = df.duplicated(["client_text", "program_text"], keep="first")
    report_rows(df.loc[duplicates], "Removed same-label duplicate pairs:")
    counts["same_label_duplicate_rows"] = int(duplicates.sum())
    return df.loc[~duplicates].copy().reset_index(drop=True), counts


def build_components(df):
    """
    Connect the original groups through shared normalized clients.
    Both split modes use this exact union-find implementation.
    """

    df = df.copy()
    parent = {key: key for key in df["group_key"].unique()}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(first, second):
        first, second = find(first), find(second)
        if first != second:
            parent[second] = first

    first_group_by_client = {}
    for _, row in df.iterrows():
        client_key, group = row["_client_key"], row["group_key"]
        if client_key in first_group_by_client:
            union(first_group_by_client[client_key], group)
        else:
            first_group_by_client[client_key] = group

    df["_component"] = df["group_key"].map(find)
    return df


def assign_splits(df, val_fraction, seed):
    """
    Preserve the original fixed split behavior. Connect groups sharing
    clients, honor old assignments, and sample new validation groups.
    """

    df = build_components(df)
    fixed_components = {}
    for component, rows in df.groupby("_component", sort=False):
        fixed = set(rows.loc[rows["_fixed_split"].ne(""), "_fixed_split"])
        if len(fixed) > 1:
            report_rows(rows, "Incompatible fixed splits connected by client/group:")
            raise ValueError(
                "A client or connected group belongs to multiple fixed old splits. "
                "The requested no-client-leakage split cannot preserve these samples; no files written."
            )
        if fixed:
            fixed_components[component] = next(iter(fixed))

    new_rows = df.loc[df["_is_new"]]
    weights = new_rows.groupby("_component", sort=False)["group_key"].nunique().to_dict()
    total_groups = int(new_rows["group_key"].nunique())
    target = math.ceil(total_groups * val_fraction)
    forced_val = sum(weight for key, weight in weights.items() if fixed_components.get(key) == "validation")
    needed = target - forced_val
    available = [key for key in weights if key not in fixed_components]
    random.Random(seed).shuffle(available)

    if needed < 0:
        raise ValueError(
            f"{forced_val} new groups share clients with fixed validation, exceeding "
            f"the requested {target}. Increase --val-fraction or resolve overlaps; no files written."
        )

    # Subset sum over randomly ordered connected components. With no
    # cross-group client duplicates, this is a random sample of groups.
    choices = {0: ()}
    for component in available:
        for amount, selected in list(choices.items()):
            new_amount = amount + weights[component]
            if new_amount <= needed and new_amount not in choices:
                choices[new_amount] = selected + (component,)
        if needed in choices:
            break
    if needed not in choices:
        raise ValueError(
            f"Cannot allocate exactly {target}/{total_groups} new groups to validation "
            "without splitting shared-client components or changing old splits. "
            "Adjust --val-fraction; no files written."
        )

    selected_val = set(choices[needed])
    df["split"] = df["_component"].map(
        lambda key: fixed_components.get(key, "validation" if key in selected_val else "training")
    )
    print(f"New groups in validation: {target}/{total_groups} (ceil of {val_fraction:.2%}); forced by shared old clients: {forced_val}.")
    assert new_rows.empty or df.loc[df["_is_new"] & df["split"].eq("validation"), "group_key"].nunique() == target
    return df


def assign_stratified_splits(df, seed, n_folds):
    """
    Fold 0 -> test, fold 1 -> validation, remaining folds -> training.
    Stratify on category + label while keeping components intact.
    All components enter the folds; flagged pairs were removed earlier.
    """

    from sklearn.model_selection import StratifiedGroupKFold

    df = build_components(df)
    assert df["_fixed_split"].eq("").all()
    assert not df["flag_review"].any(), "Flagged pairs must be excluded before splitting."
    eligible = df

    if eligible["_component"].nunique() < n_folds:
        raise ValueError(
            f"Stratified splitting needs at least {n_folds} components; no files written."
        )

    strata = eligible["pair_category"] + "_" + eligible["label"].astype(str)
    splitter = StratifiedGroupKFold(n_splits=n_folds, shuffle=True, random_state=seed)
    df["split"] = "training"
    assigned = set()

    for fold, (_, fold_positions) in enumerate(splitter.split(
        X=eligible[["label"]],
        y=strata,
        groups=eligible["_component"],
    )):
        indices = eligible.iloc[fold_positions].index
        assert assigned.isdisjoint(indices), "A row appears in multiple folds."
        assigned.update(indices)
        split = "test" if fold == 0 else "validation" if fold == 1 else "training"
        df.loc[indices, "split"] = split

    assert assigned == set(eligible.index), "Some eligible rows were not assigned to a fold."
    print(f"Components distributed across {n_folds} folds:", eligible["_component"].nunique())
    return df


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(description="Build the reviewed v0.5 dataset with group splits and no client leakage.")
    parser.add_argument("--split-mode", choices=["fixed", "stratified"], default="stratified")
    parser.add_argument("--n-folds", type=int, default=5, help="Stratified folds (at least 3); unused in fixed mode.")
    parser.add_argument("--val-fraction", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if not 0 <= args.val_fraction < 1:
        parser.error("--val-fraction must be in [0, 1).")
    if args.n_folds < 3:
        parser.error("--n-folds must be at least 3.")
    for path in [CSV_OUTPUT_PATH, EXCEL_OUTPUT_PATH]:
        if path.exists() and not args.overwrite and not args.dry_run:
            raise FileExistsError(f"{path} already exists. Use --overwrite to replace it.")

    new_pairs = load_new_pairs()
    old_pairs = load_old_pairs(args.split_mode)
    pairs = pd.concat([old_pairs, new_pairs], ignore_index=True)
    if args.split_mode == "stratified":
        pairs["_fixed_split"] = ""
    loaded = len(pairs)
    print("Loaded pairs per source:")
    print(pairs["source"].value_counts().to_string())

    if args.split_mode == "fixed":
        pairs, removed = remove_overlaps_and_duplicates(pairs)
        pairs = assign_splits(pairs, args.val_fraction, args.seed)
    else:
        print(f"Stratified mode: --val-fraction is unused; validation/test each receive one of {args.n_folds} folds.")
        pairs, removed = remove_stratified_duplicates(pairs)
        pairs = assign_stratified_splits(pairs, args.seed, args.n_folds)

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    assert pairs["label"].isin([0, 1]).all()
    assert pairs.groupby("group_key")["split"].nunique().eq(1).all()
    assert pairs.groupby("_component")["split"].nunique().eq(1).all(), "Component leakage across splits."
    assert pairs.groupby("_client_key")["split"].nunique().eq(1).all(), "Client leakage across splits."
    assert not pairs.duplicated(["client_text", "program_text"]).any()
    if args.split_mode == "fixed":
        assert len(pairs.loc[pairs["split"].eq("test")]) == 264
        assert pairs.loc[pairs["split"].eq("test"), "source"].eq("review300_test").all()
        assert pairs.loc[pairs["source"].eq("review100"), "split"].eq("validation").all()
        assert pairs.loc[pairs["source"].eq("review300_claude"), "split"].eq("training").all()
    else:
        for split in ["training", "validation", "test"]:
            split_pairs = pairs.loc[pairs["split"].eq(split)]
            assert set(split_pairs["label"]) == {0, 1}, f"{split} must contain both labels."
            assert set(split_pairs["pair_category"]) == CATEGORIES, f"{split} must contain all four categories."
        assert not pairs["flag_review"].any(), "No flag_review=True pair may remain."
    assert loaded - len(pairs) == sum(removed.values())

    pairs["_split_order"] = pairs["split"].map({"training": 0, "validation": 1, "test": 2})
    pairs = pairs.sort_values(["_split_order", "source", "_origin_id"], kind="stable").reset_index(drop=True)
    pairs["pair_id"] = [f"F{number:05d}" for number in range(1, len(pairs) + 1)]
    output = pairs[OUTPUT_COLUMNS].copy()
    assert output["pair_id"].is_unique

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print("=" * 70)
    print("FINAL POC DATASET V0.5")
    print("Split mode:", args.split_mode)
    print("=" * 70)
    print("Pairs per split:")
    print(output["split"].value_counts().to_string())
    print("\nPercentage of all pairs per split:")
    print((output["split"].value_counts(normalize=True) * 100).round(2).to_string())
    print("\nSplit x source:")
    print(pd.crosstab(output["split"], output["source"]).to_string())
    print("\nSplit x pair_category x label:")
    print(output.groupby(["split", "pair_category", "label"]).size().to_string())
    print("\nPair category x label percentages within each split (rows sum to 100%):")
    proportions = pd.crosstab(
        output["split"], [output["pair_category"], output["label"]], normalize="index",
    ) * 100
    print(proportions.round(2).to_string())
    print("\nGroups per split:")
    print(output.groupby("split")["group_key"].nunique().to_string())
    print("\nRemoved pairs:")
    for reason, count in removed.items():
        print(f"  {reason}: {count}")
    print("Total removed:", loaded - len(output))
    print("\nflag_review=True per split:")
    print(output.groupby("split")["flag_review"].sum().to_string())

    if args.split_mode == "stratified":
        difficult_masks = {
            "semi_hard_no_match": output["pair_category"].eq("semi_hard_negative") & output["label"].eq(0),
            "multi_match": output["pair_category"].isin(["easy_negative", "semi_hard_negative"]) & output["label"].eq(1),
            "non_lexical_match": output["pair_category"].eq("non_lexical_positive") & output["label"].eq(1),
            "non_lexical_no_match": output["pair_category"].eq("non_lexical_positive") & output["label"].eq(0),
            "positive_no_match": output["pair_category"].eq("positive") & output["label"].eq(0),
        }
        difficult_groups = pd.DataFrame({
            name: output.loc[mask].groupby("split")["group_key"].nunique()
            for name, mask in difficult_masks.items()
        }).reindex(["training", "validation", "test"]).fillna(0).astype(int)
        print("\nDifficult groups per split (distinct group_key; subsets may overlap):")
        print(difficult_groups.to_string())

    if args.dry_run:
        print("Dry run complete: no output files written.")
        return

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    csv_bytes = output.to_csv(index=False).encode("utf-8-sig")
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        output.to_excel(writer, sheet_name="pairs", index=False)
        sheet = writer.sheets["pairs"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in [CSV_OUTPUT_PATH, EXCEL_OUTPUT_PATH]:
        if path.exists() and not args.overwrite:
            raise FileExistsError(f"{path} already exists. Use --overwrite to replace it.")
    mode = "wb" if args.overwrite else "xb"
    with CSV_OUTPUT_PATH.open(mode) as handle:
        handle.write(csv_bytes)
    with EXCEL_OUTPUT_PATH.open(mode) as handle:
        handle.write(excel_buffer.getvalue())
    print("Saved:", CSV_OUTPUT_PATH)
    print("Saved:", EXCEL_OUTPUT_PATH)


if __name__ == "__main__":
    main()
