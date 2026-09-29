from pathlib import Path

import pandas as pd


INPUT_PATH = Path("data/output/kmin_rank_analysis.xlsx")
OUTPUT_PATH = Path("data/output/kmin_coverage_analysis.xlsx")


def main():

    # ---------------------------------------------------------
    # Load per-client positive ranks
    # ---------------------------------------------------------

    df = pd.read_excel(
        INPUT_PATH,
        sheet_name="client_positive_ranks",
    )

    required_columns = {
        "analysis_client_id",
        "expense_class",
        "same_class_program_count",
        "known_positive_rank",
    }

    missing = required_columns - set(df.columns)

    if missing:
        raise ValueError(
            f"Missing required columns: {sorted(missing)}"
        )

    # ---------------------------------------------------------
    # Calculate Coverage(K) for every class
    # ---------------------------------------------------------

    coverage_rows = []
    summary_rows = []

    for expense_class, class_df in df.groupby("expense_class"):

        class_df = class_df.copy()

        positive_ranks = (
            class_df["known_positive_rank"]
            .dropna()
            .astype(int)
        )

        if positive_ranks.empty:
            continue

        total_clients = len(positive_ranks)

        # Number of Program Expenses available in this class
        max_k = int(
            class_df["same_class_program_count"].max()
        )

        first_k_90 = None
        first_k_95 = None
        first_k_100 = None

        for k in range(1, max_k + 1):

            positives_inside = int(
                (positive_ranks <= k).sum()
            )

            coverage = (
                positives_inside / total_clients
            )

            coverage_rows.append(
                {
                    "expense_class": expense_class,
                    "k": k,
                    "known_positives_inside": positives_inside,
                    "total_known_positives": total_clients,
                    "coverage": coverage,
                    "coverage_percentage": coverage * 100,
                }
            )

            # First K reaching each coverage level

            if first_k_90 is None and coverage >= 0.90:
                first_k_90 = k

            if first_k_95 is None and coverage >= 0.95:
                first_k_95 = k

            if first_k_100 is None and coverage >= 1.00:
                first_k_100 = k

        summary_rows.append(
            {
                "expense_class": expense_class,
                "clients": total_clients,
                "same_class_program_count": max_k,
                "k_90": first_k_90,
                "k_95": first_k_95,
                "k_100": first_k_100,
                "max_positive_rank": int(
                    positive_ranks.max()
                ),
            }
        )

    # ---------------------------------------------------------
    # Create DataFrames
    # ---------------------------------------------------------

    coverage_df = pd.DataFrame(coverage_rows)

    summary_df = pd.DataFrame(summary_rows)

    # ---------------------------------------------------------
    # Save Excel
    # ---------------------------------------------------------

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with pd.ExcelWriter(
        OUTPUT_PATH,
        engine="openpyxl",
    ) as writer:

        coverage_df.to_excel(
            writer,
            sheet_name="coverage_by_k",
            index=False,
        )

        summary_df.to_excel(
            writer,
            sheet_name="coverage_summary",
            index=False,
        )

    # ---------------------------------------------------------
    # Console summary
    # ---------------------------------------------------------

    print()
    print("KMIN COVERAGE ANALYSIS COMPLETE")
    print()

    print(
        summary_df[
            [
                "expense_class",
                "clients",
                "same_class_program_count",
                "k_90",
                "k_95",
                "k_100",
            ]
        ].to_string(index=False)
    )

    print()
    print(f"Excel: {OUTPUT_PATH}")
    print()


if __name__ == "__main__":
    main()