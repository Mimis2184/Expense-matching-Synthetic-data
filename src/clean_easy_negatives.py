import shutil
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


from src.easy_negative_candidate_selection import (
    VALIDATED_NEGATIVES_CSV_PATH,
    VALIDATED_NEGATIVES_EXCEL_PATH,
    CHECKPOINT_NEGATIVES_PATH,
    assert_non_fundable_detection,
    detect_non_fundable_pair,
)


# ============================================================
# Paths
#
# The original easy-negative outputs are never modified.
# A byte-identical backup is kept under data/output/backup/.
# ============================================================

BACKUP_DIR = (
    BASE_DIR
    / "data/output/backup"
)

CLEAN_NEGATIVES_CSV_PATH = (
    BASE_DIR
    / "data/output/easy_negative_pairs_validated_clean.csv"
)

CLEAN_NEGATIVES_EXCEL_PATH = (
    BASE_DIR
    / "data/output/easy_negative_pairs_validated_clean.xlsx"
)

REMOVED_NON_FUNDABLE_PATH = (
    BASE_DIR
    / "data/output/easy_negative_removed_non_fundable.csv"
)


# ============================================================
# Helpers
# ============================================================

def backup_original(
    path: Path,
) -> Path:

    BACKUP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    backup_path = (
        BACKUP_DIR
        / f"{path.stem}.original{path.suffix}"
    )

    # Never overwrite an existing backup: the first copy is the original.
    if not backup_path.exists():
        shutil.copy2(
            path,
            backup_path,
        )

    return backup_path


# ============================================================
# Main
# ============================================================

def main() -> None:

    assert_non_fundable_detection()

    backups = [
        backup_original(VALIDATED_NEGATIVES_CSV_PATH),
        backup_original(VALIDATED_NEGATIVES_EXCEL_PATH),
    ]

    negatives_df = pd.read_csv(
        VALIDATED_NEGATIVES_CSV_PATH,
        encoding="utf-8-sig",
    )

    # --------------------------------------------------------
    # Detect non-fundable rows with the fixed exact detection
    # --------------------------------------------------------

    detections = [
        detect_non_fundable_pair(row)
        for _, row in negatives_df.iterrows()
    ]

    non_fundable_mask = pd.Series(
        [detected for detected, _ in detections],
        index=negatives_df.index,
    )

    # --------------------------------------------------------
    # Audit file of removed rows
    #
    # The final easy output has no client id. The easy checkpoint
    # holds the same negatives in the same order with ids, so the
    # ids are attached when that correspondence is verified.
    # --------------------------------------------------------

    removed_df = negatives_df[non_fundable_mask].copy()

    removed_df.insert(
        0,
        "non_fundable_source_fields",
        [
            ", ".join(fields)
            for detected, fields in detections
            if detected
        ],
    )

    checkpoint_df = pd.read_csv(
        CHECKPOINT_NEGATIVES_PATH,
        encoding="utf-8-sig",
    )

    shared_columns = [
        column
        for column in negatives_df.columns
        if column != "program_expense_id"
    ]

    checkpoint_matches = (
        len(checkpoint_df) == len(negatives_df)
        and (
            checkpoint_df["candidate_program_expense_id"].to_numpy()
            == negatives_df["program_expense_id"].to_numpy()
        ).all()
        and checkpoint_df[shared_columns].fillna("").equals(
            negatives_df[shared_columns].fillna("")
        )
    )

    if checkpoint_matches:

        removed_df.insert(
            0,
            "analysis_client_id",
            checkpoint_df.loc[
                removed_df.index,
                "analysis_client_id",
            ].to_numpy(),
        )

    clean_df = negatives_df[~non_fundable_mask].copy()

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    remaining = [
        index
        for index, row in clean_df.iterrows()
        if detect_non_fundable_pair(row)[0]
    ]

    assert not remaining, (
        "Cleaned easy negatives still contain non-fundable values: "
        f"{remaining[:10]}"
    )

    assert len(clean_df) + len(removed_df) == len(negatives_df)

    assert (clean_df["label"] == 0).all()

    assert list(clean_df.columns) == list(negatives_df.columns)

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    clean_df.to_csv(
        CLEAN_NEGATIVES_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    clean_df.to_excel(
        CLEAN_NEGATIVES_EXCEL_PATH,
        index=False,
        engine="openpyxl",
    )

    removed_df.to_csv(
        REMOVED_NON_FUNDABLE_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    print("=" * 70)
    print("CLEAN EASY NEGATIVES")
    print("=" * 70)
    print("Easy negatives before:", len(negatives_df))
    print("Non-fundable removed:", len(removed_df))
    print("Easy negatives after:", len(clean_df))
    print()
    print("Removed rows by matched field(s):")
    print(
        removed_df["non_fundable_source_fields"]
        .value_counts()
        .to_string()
    )
    print()
    print("Client ids attached to audit file:", checkpoint_matches)
    print()

    for backup_path in backups:
        print("Backup:", backup_path)

    print("Clean CSV:", CLEAN_NEGATIVES_CSV_PATH)
    print("Clean Excel:", CLEAN_NEGATIVES_EXCEL_PATH)
    print("Removed rows:", REMOVED_NON_FUNDABLE_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
