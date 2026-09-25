from pathlib import Path

import pandas as pd
import sys 

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import CLIENT_DATA_PATH, CLEAN_CLIENT_DATA_PATH


def prepare_client_data() -> None:
    print(f"Reading: {CLIENT_DATA_PATH}")

    # 1. Διαβάζουμε το πραγματικό client dataset
    df = pd.read_csv(
        CLIENT_DATA_PATH,
        sep=";",
        encoding="utf-8-sig",
        low_memory=False,
    )

    print(f"Initial rows: {len(df):,}")

    # 2. Κρατάμε μόνο τα πεδία που μας ενδιαφέρουν
    df = df[
        [
            "expense_name",
            "expense_description",
            "expense_category",
        ]
    ].copy()

    # 3. Αφαιρούμε rows με missing απαραίτητα πεδία
    df = df.dropna(
        subset=[
            "expense_name",
            "expense_description",
            "expense_category",
        ]
    )

    print(f"After removing nulls: {len(df):,}")

    # 4. Καθαρίζουμε μόνο περιττά κενά.
    # ΔΕΝ αλλάζουμε lowercase/uppercase,
    for column in [
        "expense_name",
        "expense_description",
        "expense_category",
    ]:
        df[column] = df[column].astype(str).str.strip()

    # 5. Αφαιρούμε κενά strings
    df = df[
        (df["expense_name"] != "")
        & (df["expense_description"] != "")
        & (df["expense_category"] != "")
    ].copy()

    # 6. Αφαιρούμε budget/grouping lines όπως:
    # [01] - ΠΡΟΥΠΟΛΟΓΙΣΜΟΣ ...
    df = df[
        ~df["expense_name"].str.startswith("[")
    ].copy()

    print(f"After removing budget lines: {len(df):,}")

    # 7. Αφαιρούμε ακριβή duplicate client examples
    df = df.drop_duplicates(
        subset=[
            "expense_name",
            "expense_description",
            "expense_category",
        ]
    ).copy()

    print(f"After deduplication: {len(df):,}")

    # 8. Μετονομάζουμε στα πεδία που θέλει το τελικό project
    df = df.rename(
        columns={
            "expense_name": "expense_title",
            "expense_category": "expense_class",
        }
    )

    # 9. Κρατάμε την τελική σειρά στηλών
    df = df[
        [
            "expense_title",
            "expense_description",
            "expense_class",
        ]
    ].reset_index(drop=True)

    # 10. Αποθήκευση
    CLEAN_CLIENT_DATA_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df.to_csv(
        CLEAN_CLIENT_DATA_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print(f"Saved: {CLEAN_CLIENT_DATA_PATH}")
    print(f"Final rows: {len(df):,}")
    print(f"Expense classes: {df['expense_class'].nunique()}")

    print("\nRows per expense_class:")
    print(df["expense_class"].value_counts().to_string())


if __name__ == "__main__":
    prepare_client_data()