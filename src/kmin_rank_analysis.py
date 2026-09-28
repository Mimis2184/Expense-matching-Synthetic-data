from pathlib import Path
import sys

import numpy as np
import pandas as pd


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(BASE_DIR),
)


# ============================================================
# Config
# ============================================================

from config import (
    EMBEDDING_DISTANCE_EXCEL_PATH,
    KMIN_ANALYSIS_EXCEL_PATH,
)


# ============================================================
# Main
# ============================================================

def main() -> None:

    # ========================================================
    # Load client-level embedding analysis
    # ========================================================

    client_df = pd.read_excel(
        EMBEDDING_DISTANCE_EXCEL_PATH,
        sheet_name="client_summary",
    )

    print(
        f"Clients loaded: "
        f"{len(client_df)}"
    )

    # ========================================================
    # Required columns
    # ========================================================

    required_columns = [
        "analysis_client_id",
        "expense_class",
        "known_positive_rank",
        "known_positive_distance",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in client_df.columns
    ]

    if missing_columns:
        raise ValueError(
            "Missing required columns: "
            f"{missing_columns}"
        )

    # ========================================================
    # Remove rows where positive rank is unavailable
    # ========================================================

    valid_df = client_df[
        client_df[
            "known_positive_rank"
        ].notna()
    ].copy()

    valid_df[
        "known_positive_rank"
    ] = pd.to_numeric(
        valid_df[
            "known_positive_rank"
        ],
        errors="raise",
    )

    print(
        f"Clients with known positive rank: "
        f"{len(valid_df)}"
    )

    # ========================================================
    # Class-level rank statistics
    # ========================================================

    summary_rows = []

    for expense_class, class_df in valid_df.groupby(
        "expense_class",
        sort=True,
    ):

        ranks = class_df[
            "known_positive_rank"
        ]

        summary_rows.append(
            {
                "expense_class":
                    expense_class,

                "clients":
                    len(class_df),

                "min_positive_rank":
                    int(
                        ranks.min()
                    ),

                "median_positive_rank":
                    float(
                        ranks.median()
                    ),

                "p90_positive_rank":
                    float(
                        np.percentile(
                            ranks,
                            90,
                        )
                    ),

                "p95_positive_rank":
                    float(
                        np.percentile(
                            ranks,
                            95,
                        )
                    ),

                "max_positive_rank":
                    int(
                        ranks.max()
                    ),

                "mean_positive_rank":
                    float(
                        ranks.mean()
                    ),
            }
        )

    class_summary_df = pd.DataFrame(
        summary_rows
    )

    # ========================================================
    # Rounded / conservative integer percentiles
    #
    # Example:
    # P95 = 4.25
    # means Top 5 if we need an integer neighbourhood size.
    #
    # These are NOT automatically Kmin.
    # They are only useful candidate values for discussion.
    # ========================================================

    class_summary_df[
        "p90_rank_ceiling"
    ] = np.ceil(
        class_summary_df[
            "p90_positive_rank"
        ]
    ).astype(int)

    class_summary_df[
        "p95_rank_ceiling"
    ] = np.ceil(
        class_summary_df[
            "p95_positive_rank"
        ]
    ).astype(int)

    # ========================================================
    # Rank distribution per class
    #
    # Example:
    #
    # class A, rank 1 -> 20 clients
    # class A, rank 2 -> 10 clients
    # class A, rank 3 -> 5 clients
    #
    # Useful for seeing the actual distribution.
    # ========================================================

    rank_distribution_df = (
        valid_df
        .groupby(
            [
                "expense_class",
                "known_positive_rank",
            ]
        )
        .size()
        .reset_index(
            name="clients_at_rank"
        )
    )

    # ========================================================
    # Percentage of clients at each rank
    # ========================================================

    total_per_class = (
        valid_df
        .groupby(
            "expense_class"
        )
        .size()
        .rename(
            "total_clients"
        )
    )

    rank_distribution_df = (
        rank_distribution_df
        .merge(
            total_per_class,
            on="expense_class",
            how="left",
        )
    )

    rank_distribution_df[
        "percentage_of_class"
    ] = (
        rank_distribution_df[
            "clients_at_rank"
        ]
        /
        rank_distribution_df[
            "total_clients"
        ]
        *
        100
    )

    # ========================================================
    # Cumulative percentage
    #
    # Tells us:
    # "What percentage of known positives is contained
    #  inside Top-K?"
    #
    # This will be very useful when deciding Kmin.
    # ========================================================

    rank_distribution_df = (
        rank_distribution_df
        .sort_values(
            [
                "expense_class",
                "known_positive_rank",
            ]
        )
        .reset_index(drop=True)
    )

    rank_distribution_df[
        "cumulative_clients"
    ] = (
        rank_distribution_df
        .groupby(
            "expense_class"
        )[
            "clients_at_rank"
        ]
        .cumsum()
    )

    rank_distribution_df[
        "cumulative_percentage"
    ] = (
        rank_distribution_df[
            "cumulative_clients"
        ]
        /
        rank_distribution_df[
            "total_clients"
        ]
        *
        100
    )

    # ========================================================
    # Detailed client data
    # ========================================================

    client_details_df = valid_df[
        [
            "analysis_client_id",
            "expense_class",
            "same_class_program_count",
            "known_positive_rank",
            "known_positive_distance",
        ]
    ].copy()

    client_details_df = (
        client_details_df
        .sort_values(
            [
                "expense_class",
                "known_positive_rank",
            ]
        )
        .reset_index(drop=True)
    )

    # ========================================================
    # Save Excel
    # ========================================================

    KMIN_ANALYSIS_EXCEL_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with pd.ExcelWriter(
        KMIN_ANALYSIS_EXCEL_PATH,
        engine="openpyxl",
    ) as writer:

        # ----------------------------------------------------
        # Main sheet:
        # one row per expense class
        # ----------------------------------------------------

        class_summary_df.to_excel(
            writer,
            sheet_name="class_rank_summary",
            index=False,
        )

        # ----------------------------------------------------
        # Actual rank distribution
        # ----------------------------------------------------

        rank_distribution_df.to_excel(
            writer,
            sheet_name="rank_distribution",
            index=False,
        )

        # ----------------------------------------------------
        # Individual client results
        # ----------------------------------------------------

        client_details_df.to_excel(
            writer,
            sheet_name="client_positive_ranks",
            index=False,
        )

    # ========================================================
    # Terminal output
    # ========================================================

    print()
    print("=" * 70)
    print(
        "KMIN RANK ANALYSIS COMPLETE"
    )
    print("=" * 70)

    print(
        f"Expense classes analysed: "
        f"{class_summary_df['expense_class'].nunique()}"
    )

    print()

    print(
        class_summary_df.to_string(
            index=False
        )
    )

    print()

    print(
        "Excel:"
    )

    print(
        KMIN_ANALYSIS_EXCEL_PATH
    )

    print("=" * 70)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    main()