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


from config import (
    OUTPUT_DATA_DIR,
    POSITIVE_PAIRS_VALIDATED_CSV_PATH,
    POSITIVE_PAIRS_CLEAN_CSV_PATH,
)

from src.easy_negative_candidate_selection import (
    assert_non_fundable_detection,
    detect_non_fundable_pair,
)


# ============================================================
# Paths
#
# The original validated positives are never modified.
# A byte-identical backup is kept under data/output/backup/.
# ============================================================

POSITIVE_PAIRS_VALIDATED_EXCEL_PATH = (
    OUTPUT_DATA_DIR
    / "positive_pairs_validated.xlsx"
)

BACKUP_DIR = (
    BASE_DIR
    / "data/output/backup"
)

CLEAN_POSITIVES_CSV_PATH = POSITIVE_PAIRS_CLEAN_CSV_PATH

CLEAN_POSITIVES_EXCEL_PATH = (
    OUTPUT_DATA_DIR
    / "positive_pairs_validated_clean.xlsx"
)

REMOVED_NON_FUNDABLE_PATH = (
    BASE_DIR
    / "data/output/positive_removed_non_fundable.csv"
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
        backup_original(POSITIVE_PAIRS_VALIDATED_CSV_PATH),
        backup_original(POSITIVE_PAIRS_VALIDATED_EXCEL_PATH),
    ]

    positives_df = pd.read_csv(
        POSITIVE_PAIRS_VALIDATED_CSV_PATH,
        encoding="utf-8-sig",
    )

    # --------------------------------------------------------
    # Keep the existing analysis_client_id
    #
    # embedding_distance_analysis.py assigns it as row index + 1
    # of the validated positives file. It is attached here before
    # any row is removed, so the remaining clients keep the same
    # ids used by the distance analysis and the negative outputs.
    # --------------------------------------------------------

    positives_df = positives_df.reset_index(drop=True)

    positives_df.insert(
        0,
        "analysis_client_id",
        positives_df.index + 1,
    )

    # --------------------------------------------------------
    # Detect non-fundable rows with the exact detection
    # --------------------------------------------------------

    detections = [
        detect_non_fundable_pair(row)
        for _, row in positives_df.iterrows()
    ]

    non_fundable_mask = pd.Series(
        [detected for detected, _ in detections],
        index=positives_df.index,
    )

    removed_df = positives_df[non_fundable_mask].copy()

    removed_df.insert(
        1,
        "non_fundable_source_fields",
        [
            ", ".join(fields)
            for detected, fields in detections
            if detected
        ],
    )

    clean_df = positives_df[~non_fundable_mask].copy()

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    remaining = [
        index
        for index, row in clean_df.iterrows()
        if detect_non_fundable_pair(row)[0]
    ]

    assert not remaining, (
        "Cleaned positives still contain non-fundable values: "
        f"{remaining[:10]}"
    )

    assert len(clean_df) + len(removed_df) == len(positives_df)

    assert (clean_df["label"] == 1).all()

    assert clean_df["analysis_client_id"].is_unique

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    clean_df.to_csv(
        CLEAN_POSITIVES_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    clean_df.to_excel(
        CLEAN_POSITIVES_EXCEL_PATH,
        index=False,
        engine="openpyxl",
    )

    removed_df.to_csv(
        REMOVED_NON_FUNDABLE_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    print("=" * 70)
    print("CLEAN POSITIVE PAIRS")
    print("=" * 70)
    print("Positives before:", len(positives_df))
    print("Non-fundable removed:", len(removed_df))
    print("Positives after:", len(clean_df))
    print()
    print("Removed rows by matched field(s):")
    print(
        removed_df["non_fundable_source_fields"]
        .value_counts()
        .to_string()
    )
    print()

    for backup_path in backups:
        print("Backup:", backup_path)

    print("Clean CSV:", CLEAN_POSITIVES_CSV_PATH)
    print("Clean Excel:", CLEAN_POSITIVES_EXCEL_PATH)
    print("Removed rows:", REMOVED_NON_FUNDABLE_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
