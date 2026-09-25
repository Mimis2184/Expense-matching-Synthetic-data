from pathlib import Path

import pandas as pd


# ============================================================
# Paths
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

POSITIVE_PAIRS_PATH = (
    BASE_DIR
    / "data"
    / "output"
    / "positive_pairs_test.csv"
)

PROGRAM_EXPENSES_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "program_expenses_clean.csv"
)

OUTPUT_PATH = (
    BASE_DIR
    / "data"
    / "output"
    / "validator_stress_test_candidates.xlsx"
)


# ============================================================
# Helpers
# ============================================================

def clean_value(value) -> str:

    if pd.isna(value):
        return ""

    return str(value).strip()


def normalize_class(value) -> str:

    return clean_value(value).casefold()


# ============================================================
# Main
# ============================================================

def main() -> None:

    positive_df = pd.read_csv(
        POSITIVE_PAIRS_PATH,
        encoding="utf-8-sig",
    )

    program_df = pd.read_csv(
        PROGRAM_EXPENSES_PATH,
        encoding="utf-8-sig",
    )

    # --------------------------------------------------------
    # 1. VALID cases
    # --------------------------------------------------------

    n_valid = min(
        5,
        len(positive_df),
    )

    valid_cases = positive_df.sample(
        n=n_valid,
        random_state=42,
    ).copy()

    valid_cases["stress_test_type"] = "VALID_CASE"
    valid_cases["expected_verdict"] = "VALID"


    # --------------------------------------------------------
    # 2. INVALID CANDIDATES
    #
    # Same client.
    # Same expense class.
    # Different program Category/Subcategory.
    #
    # IMPORTANT:
    # these are candidates and must be manually checked.
    # --------------------------------------------------------

    invalid_rows = []

    for _, client_row in valid_cases.iterrows():

        client_class = normalize_class(
            client_row["client_expense_class"]
        )

        original_category = clean_value(
            client_row["program_category"]
        )

        original_subcategory = clean_value(
            client_row["program_subcategory"]
        )

        candidates = program_df[
            program_df["expense_class"]
            .apply(normalize_class)
            .eq(client_class)
        ].copy()

        # Different semantic target from the original positive anchor
        candidates = candidates[
            (
                candidates["category"]
                .fillna("")
                .astype(str)
                .str.strip()
                .ne(original_category)
            )
            |
            (
                candidates["subcategory"]
                .fillna("")
                .astype(str)
                .str.strip()
                .ne(original_subcategory)
            )
        ]

        if candidates.empty:
            continue

        # Deterministic candidate selection
        alternative = candidates.sample(
            n=1,
            random_state=42,
        ).iloc[0]

        invalid_rows.append(
            {
                "client_expense_title":
                    client_row["client_expense_title"],

                "client_expense_description":
                    client_row["client_expense_description"],

                "client_expense_class":
                    client_row["client_expense_class"],

                "program_expense_id":
                    "",

                "program_title":
                    clean_value(
                        alternative.get(
                            "program_title",
                            "",
                        )
                    ),

                "subprogram_name":
                    clean_value(
                        alternative.get(
                            "subprogram_name",
                            "",
                        )
                    ),

                "program_category":
                    clean_value(
                        alternative["category"]
                    ),

                "program_subcategory":
                    clean_value(
                        alternative["subcategory"]
                    ),

                "program_expense_description":
                    clean_value(
                        alternative[
                            "expense_description"
                        ]
                    ),

                "program_expense_class":
                    clean_value(
                        alternative["expense_class"]
                    ),

                "label":
                    "",

                "stress_test_type":
                    "INVALID_CANDIDATE",

                # Deliberately blank until we inspect it
                "expected_verdict":
                    "",
            }
        )


    invalid_cases = pd.DataFrame(
        invalid_rows
    )


    # --------------------------------------------------------
    # 3. UNCERTAIN cases
    #
    # We deliberately remove semantic program information.
    # These are artificial stress-test cases only.
    # --------------------------------------------------------

    n_uncertain = min(
        3,
        len(valid_cases),
    )

    uncertain_cases = (
        valid_cases
        .head(n_uncertain)
        .copy()
    )

    uncertain_cases[
        "program_category"
    ] = ""

    uncertain_cases[
        "program_subcategory"
    ] = ""

    uncertain_cases[
        "program_expense_description"
    ] = ""

    uncertain_cases[
        "stress_test_type"
    ] = "UNCERTAIN_CASE"

    uncertain_cases[
        "expected_verdict"
    ] = "UNCERTAIN"


    # --------------------------------------------------------
    # Combine
    # --------------------------------------------------------

    stress_df = pd.concat(
        [
            valid_cases,
            invalid_cases,
            uncertain_cases,
        ],
        ignore_index=True,
    )


    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    stress_df.to_excel(
        OUTPUT_PATH,
        index=False,
        engine="openpyxl",
    )


    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("VALIDATOR STRESS TEST CANDIDATES")
    print("=" * 60)

    print(
        stress_df[
            "stress_test_type"
        ]
        .value_counts()
        .to_string()
    )

    print()

    print(
        f"Saved:\n{OUTPUT_PATH}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()