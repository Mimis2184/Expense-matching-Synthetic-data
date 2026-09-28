from pathlib import Path
import sys
from typing import Any, cast

import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer


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
    POSITIVE_PAIRS_VALIDATED_CSV_PATH,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DISTANCE_CSV_PATH,
    EMBEDDING_DISTANCE_EXCEL_PATH,
)


# ============================================================
# Program Expenses input
# ============================================================

PROGRAM_DATA_PATH = (
    BASE_DIR
    / "data"
    / "processed"
    / "program_expenses_clean.csv"
)


# ============================================================
# Helpers
# ============================================================

def clean_value(value: Any) -> str:
    """
    Convert missing values to an empty string
    and remove surrounding whitespace.
    """

    if pd.isna(value):
        return ""

    return str(value).strip()


def normalize_class(value: Any) -> str:
    """
    Normalize expense class only for comparison.
    """

    return clean_value(value).casefold()


def to_int(value: object) -> int:
    """
    Convert a pandas / NumPy scalar to int.

    cast(Any, ...) is mainly used so Pylance knows
    that the value is expected to be a scalar here.
    """

    return int(
        cast(
            Any,
            value,
        )
    )


def to_float(value: object) -> float:
    """
    Convert a pandas / NumPy scalar to float.
    """

    return float(
        cast(
            Any,
            value,
        )
    )


def prepare_program_expense_ids(
    program_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Ensure every Program Expense has a program_expense_id.

    If program_expenses_clean.csv already contains the ID,
    use it.

    Otherwise recreate the current project logic:
    row index + 1.
    """

    program_df = program_df.copy()

    if "program_expense_id" not in program_df.columns:

        program_df = (
            program_df
            .reset_index(drop=True)
        )

        program_df[
            "program_expense_id"
        ] = (
            program_df.index + 1
        )

    else:

        program_df[
            "program_expense_id"
        ] = (
            pd.to_numeric(
                program_df[
                    "program_expense_id"
                ],
                errors="raise",
            )
            .astype(int)
        )

    return program_df


# ============================================================
# Text construction for E5
# ============================================================

def build_client_text(
    row: pd.Series,
) -> str:
    """
    Client Expense becomes an E5 query.

    expense_class is NOT included in the embedding text.
    It is used separately as a hard filtering rule.
    """

    title = clean_value(
        row["client_expense_title"]
    )

    description = clean_value(
        row["client_expense_description"]
    )

    return (
        f"query: "
        f"Title: {title}. "
        f"Description: {description}"
    )


def build_program_text(
    row: pd.Series,
) -> str:
    """
    Program Expense becomes an E5 passage.

    Semantic information:
    - Category
    - Subcategory
    - Expense Description
    """

    category = clean_value(
        row["category"]
    )

    subcategory = clean_value(
        row["subcategory"]
    )

    description = clean_value(
        row["expense_description"]
    )

    return (
        f"passage: "
        f"Category: {category}. "
        f"Subcategory: {subcategory}. "
        f"Description: {description}"
    )


# ============================================================
# Main
# ============================================================

def main() -> None:

    # ========================================================
    # Load validated positive clients
    # ========================================================

    positive_df = pd.read_csv(
        POSITIVE_PAIRS_VALIDATED_CSV_PATH,
        encoding="utf-8-sig",
    )

    # ========================================================
    # Load real Program Expenses
    # ========================================================

    program_df = pd.read_csv(
        PROGRAM_DATA_PATH,
        encoding="utf-8-sig",
    )

    program_df = prepare_program_expense_ids(
        program_df
    )

    print(
        f"Validated positive clients: "
        f"{len(positive_df)}"
    )

    print(
        f"Program expenses: "
        f"{len(program_df)}"
    )

    # ========================================================
    # Required-column checks
    # ========================================================

    required_positive_columns = [
        "program_expense_id",
        "client_expense_title",
        "client_expense_description",
        "client_expense_class",
    ]

    required_program_columns = [
        "program_expense_id",
        "category",
        "subcategory",
        "expense_description",
        "expense_class",
    ]

    missing_positive = [
        column
        for column in required_positive_columns
        if column not in positive_df.columns
    ]

    if missing_positive:
        raise ValueError(
            "Missing columns in positive dataset: "
            f"{missing_positive}"
        )

    missing_program = [
        column
        for column in required_program_columns
        if column not in program_df.columns
    ]

    if missing_program:
        raise ValueError(
            "Missing columns in program dataset: "
            f"{missing_program}"
        )

    # ========================================================
    # Temporary analysis client ID
    #
    # NOT a permanent business/client ID.
    # Used only for tracing rows in this analysis.
    # ========================================================

    positive_df = (
        positive_df
        .reset_index(drop=True)
    )

    positive_df[
        "analysis_client_id"
    ] = (
        positive_df.index + 1
    )

    # ========================================================
    # Build embedding texts
    # ========================================================

    positive_df[
        "embedding_text"
    ] = positive_df.apply(
        build_client_text,
        axis=1,
    )

    program_df[
        "embedding_text"
    ] = program_df.apply(
        build_program_text,
        axis=1,
    )

    # ========================================================
    # Load pretrained embedding model
    # ========================================================

    print()
    print(
        f"Loading embedding model: "
        f"{EMBEDDING_MODEL_NAME}"
    )

    model = SentenceTransformer(
        EMBEDDING_MODEL_NAME
    )

    # ========================================================
    # Create Client embeddings
    # ========================================================

    print()
    print(
        "Creating client embeddings..."
    )

    client_embeddings = model.encode(
        positive_df[
            "embedding_text"
        ].tolist(),
        batch_size=EMBEDDING_BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,
    )

    # ========================================================
    # Create Program embeddings
    # ========================================================

    print()
    print(
        "Creating program embeddings..."
    )

    program_embeddings = model.encode(
        program_df[
            "embedding_text"
        ].tolist(),
        batch_size=EMBEDDING_BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,
    )

    # ========================================================
    # Results
    # ========================================================

    results: list[dict[str, Any]] = []

    total_clients = len(
        positive_df
    )

    # ========================================================
    # Compare each client ONLY with Programs of same class
    # ========================================================

    for client_position, (_, client_row) in enumerate(
        positive_df.iterrows(),
        start=0,
    ):

        print(
            f"Calculating distances "
            f"{client_position + 1}/{total_clients}",
            end="\r",
            flush=True,
        )

        # ----------------------------------------------------
        # Client class
        # ----------------------------------------------------

        client_class = normalize_class(
            client_row[
                "client_expense_class"
            ]
        )

        # ----------------------------------------------------
        # Known positive Program Expense
        # ----------------------------------------------------

        positive_program_id = to_int(
            client_row[
                "program_expense_id"
            ]
        )

        # ----------------------------------------------------
        # Same expense_class Programs ONLY
        # ----------------------------------------------------

        same_class_indexes = (
            program_df.index[
                program_df[
                    "expense_class"
                ]
                .apply(normalize_class)
                .eq(client_class)
            ]
            .tolist()
        )

        if not same_class_indexes:
            continue

        # ----------------------------------------------------
        # Current client embedding
        # ----------------------------------------------------

        client_vector = (
            client_embeddings[
                client_position
            ]
        )

        # ----------------------------------------------------
        # Same-class Program embeddings
        # ----------------------------------------------------

        class_program_vectors = (
            program_embeddings[
                same_class_indexes
            ]
        )

        # ----------------------------------------------------
        # Cosine similarity
        #
        # Since embeddings are normalized:
        #
        # dot product = cosine similarity
        # ----------------------------------------------------

        similarities = (
            class_program_vectors
            @ client_vector
        )

        # ----------------------------------------------------
        # Cosine distance
        #
        # distance = 1 - cosine similarity
        #
        # SMALL distance = more semantic similarity
        # LARGE distance = less semantic similarity
        # ----------------------------------------------------

        distances = (
            1.0
            -
            similarities
        )

        # ----------------------------------------------------
        # Sort nearest -> farthest
        # ----------------------------------------------------

        order = np.argsort(
            distances
        )

        # ----------------------------------------------------
        # Save every same-class comparison
        # ----------------------------------------------------

        for rank_position, ordered_position in enumerate(
            order,
            start=1,
        ):

            ordered_position_int = to_int(
                ordered_position
            )

            program_index = to_int(
                same_class_indexes[
                    ordered_position_int
                ]
            )

            program_row = cast(
                pd.Series,
                program_df.loc[
                    program_index
                ],
            )

            program_id = to_int(
                program_row[
                    "program_expense_id"
                ]
            )

            similarity = to_float(
                similarities[
                    ordered_position_int
                ]
            )

            distance = to_float(
                distances[
                    ordered_position_int
                ]
            )

            results.append(
                {
                    "analysis_client_id":
                        to_int(
                            client_row[
                                "analysis_client_id"
                            ]
                        ),

                    "positive_program_expense_id":
                        positive_program_id,

                    "candidate_program_expense_id":
                        program_id,

                    "client_expense_title":
                        clean_value(
                            client_row[
                                "client_expense_title"
                            ]
                        ),

                    "client_expense_description":
                        clean_value(
                            client_row[
                                "client_expense_description"
                            ]
                        ),

                    "expense_class":
                        clean_value(
                            client_row[
                                "client_expense_class"
                            ]
                        ),

                    "program_category":
                        clean_value(
                            program_row[
                                "category"
                            ]
                        ),

                    "program_subcategory":
                        clean_value(
                            program_row[
                                "subcategory"
                            ]
                        ),

                    "program_expense_description":
                        clean_value(
                            program_row[
                                "expense_description"
                            ]
                        ),

                    "cosine_similarity":
                        similarity,

                    "cosine_distance":
                        distance,

                    "distance_rank":
                        rank_position,

                    "is_known_positive":
                        (
                            program_id
                            ==
                            positive_program_id
                        ),
                }
            )

    print()

    # ========================================================
    # All same-class distances
    # ========================================================

    distance_df = pd.DataFrame(
        results
    )

    if distance_df.empty:
        raise ValueError(
            "No embedding distances were generated."
        )

    # ========================================================
    # Client-level summary
    #
    # d1 = nearest Program
    # d2 = second-nearest Program
    # ...
    #
    # These are measurements.
    # They are NOT Kmin yet.
    # ========================================================

    client_summary_rows: list[
        dict[str, Any]
    ] = []

    for client_id, group in distance_df.groupby(
        "analysis_client_id",
        sort=False,
    ):

        group = (
            group
            .sort_values(
                "distance_rank"
            )
            .reset_index(drop=True)
        )

        first_row = cast(
            pd.Series,
            group.iloc[0],
        )

        positive_rows = group[
            group[
                "is_known_positive"
            ]
            == True
        ]

        summary: dict[str, Any] = {
            "analysis_client_id":
                to_int(
                    client_id
                ),

            "expense_class":
                clean_value(
                    first_row[
                        "expense_class"
                    ]
                ),

            "same_class_program_count":
                len(group),
        }

        # ----------------------------------------------------
        # Store first 10 minimum distances
        #
        # This DOES NOT mean K = 10.
        # Only for analysis/inspection.
        # ----------------------------------------------------

        for k in range(
            1,
            11,
        ):

            column_name = (
                f"d{k}"
            )

            if len(group) >= k:

                distance_value = (
                    group.iloc[
                        k - 1
                    ][
                        "cosine_distance"
                    ]
                )

                summary[
                    column_name
                ] = to_float(
                    distance_value
                )

            else:

                summary[
                    column_name
                ] = np.nan

        # ----------------------------------------------------
        # Known validated positive
        # ----------------------------------------------------

        if not positive_rows.empty:

            known_positive_row = cast(
                pd.Series,
                positive_rows.iloc[0],
            )

            summary[
                "known_positive_distance"
            ] = to_float(
                known_positive_row[
                    "cosine_distance"
                ]
            )

            summary[
                "known_positive_rank"
            ] = to_int(
                known_positive_row[
                    "distance_rank"
                ]
            )

        else:

            summary[
                "known_positive_distance"
            ] = np.nan

            summary[
                "known_positive_rank"
            ] = np.nan

        client_summary_rows.append(
            summary
        )

    client_summary_df = pd.DataFrame(
        client_summary_rows
    )

    # ========================================================
    # Class-level summary
    # ========================================================

    class_summary_df = (
        client_summary_df
        .groupby(
            "expense_class",
            dropna=False,
        )
        .agg(
            clients=(
                "analysis_client_id",
                "count",
            ),

            average_same_class_programs=(
                "same_class_program_count",
                "mean",
            ),

            mean_d1=(
                "d1",
                "mean",
            ),

            median_d1=(
                "d1",
                "median",
            ),

            mean_d2=(
                "d2",
                "mean",
            ),

            median_d2=(
                "d2",
                "median",
            ),

            mean_d3=(
                "d3",
                "mean",
            ),

            median_d3=(
                "d3",
                "median",
            ),

            mean_positive_distance=(
                "known_positive_distance",
                "mean",
            ),

            median_positive_distance=(
                "known_positive_distance",
                "median",
            ),

            mean_positive_rank=(
                "known_positive_rank",
                "mean",
            ),

            median_positive_rank=(
                "known_positive_rank",
                "median",
            ),
        )
        .reset_index()
    )

    # ========================================================
    # Save CSV
    # ========================================================

    EMBEDDING_DISTANCE_CSV_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    distance_df.to_csv(
        EMBEDDING_DISTANCE_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    # ========================================================
    # Save Excel with 3 sheets
    # ========================================================

    with pd.ExcelWriter(
        EMBEDDING_DISTANCE_EXCEL_PATH,
        engine="openpyxl",
    ) as writer:

        # ----------------------------------------------------
        # Sheet 1: all pairwise same-class distances
        # ----------------------------------------------------

        distance_df.to_excel(
            writer,
            sheet_name="all_distances",
            index=False,
        )

        # ----------------------------------------------------
        # Sheet 2: per-client nearest distance summary
        # ----------------------------------------------------

        client_summary_df.to_excel(
            writer,
            sheet_name="client_summary",
            index=False,
        )

        # ----------------------------------------------------
        # Sheet 3: per-class summary
        # ----------------------------------------------------

        class_summary_df.to_excel(
            writer,
            sheet_name="class_summary",
            index=False,
        )

    # ========================================================
    # Terminal summary
    # ========================================================

    print()
    print("=" * 70)
    print(
        "EMBEDDING DISTANCE ANALYSIS COMPLETE"
    )
    print("=" * 70)

    print(
        f"Clients analysed: "
        f"{len(client_summary_df)}"
    )

    print(
        f"Same-class pairs analysed: "
        f"{len(distance_df)}"
    )

    print(
        f"Expense classes: "
        f"{client_summary_df['expense_class'].nunique()}"
    )

    print()

    print(
        "CSV:"
    )

    print(
        EMBEDDING_DISTANCE_CSV_PATH
    )

    print()

    print(
        "Excel:"
    )

    print(
        EMBEDDING_DISTANCE_EXCEL_PATH
    )

    print("=" * 70)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    main()