import argparse
import io
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# Project root / offline execution
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(BASE_DIR),
)

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from config import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MODEL_NAME,
    OUTPUT_DATA_DIR,
    PROCESSED_DATA_DIR,
)

from src.embedding_distance_analysis import (
    build_client_text,
    build_program_text,
    clean_value,
    normalize_class,
)


# ============================================================
# Negative candidates (v0.5)
#
# Use every new client row and rank same-class program expenses
# with the same E5 text builders as the original distance study.
# Exclude its positive anchor and exact stripped-text twins.
#
# Select nearest semi-hard candidates, then one unused easy
# candidate just outside K95, or the farthest unused fallback.
# Ranks are computed AFTER exclusions, within eligible candidates.
#
# These are candidates only: no label is assigned. Claude will
# review them later against matching_guidelines_v0.5.md.
# No LLM call, no API call, no existing input data is modified.
# Dry run computes everything but never writes an output file.
# ============================================================

CLIENT_PATH = OUTPUT_DATA_DIR / "positive_pairs_v05_uncovered.csv"
PROGRAM_PATH = PROCESSED_DATA_DIR / "program_expenses_clean.csv"
COVERAGE_PATH = OUTPUT_DATA_DIR / "kmin_coverage_analysis.xlsx"

CSV_OUTPUT_PATH = OUTPUT_DATA_DIR / "negative_candidates_v05.csv"
EXCEL_OUTPUT_PATH = OUTPUT_DATA_DIR / "negative_candidates_v05.xlsx"

PROGRAM_TEXT_COLUMNS = ["category", "subcategory", "expense_description"]
NON_FUNDABLE_CLASS = normalize_class("Μη επιλέξιμες δαπάνες")

OUTPUT_COLUMNS = [
    "candidate_id",
    "pair_category",
    "source_row",
    "client_expense_title",
    "client_expense_description",
    "client_expense_class",
    "anchor_program_expense_id",
    "program_expense_id",
    "program_title",
    "subprogram_name",
    "program_category",
    "program_subcategory",
    "program_expense_description",
    "program_expense_class",
    "cosine_similarity",
    "rank",
    "k95",
    "easy_strategy",
    "generation_run",
]


# ============================================================
# Inputs and helpers
# ============================================================

def require_columns(df, columns, path) -> None:

    missing = sorted(set(columns) - set(df.columns))

    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")


def program_text_key(row) -> tuple:
    """
    Match each field separately, after strip only.
    Do not casefold or otherwise merge different expense texts.
    """

    return tuple(clean_value(row[column]) for column in PROGRAM_TEXT_COLUMNS)


def load_inputs():

    clients = pd.read_csv(CLIENT_PATH, encoding="utf-8-sig").reset_index(drop=True)
    programs = pd.read_csv(PROGRAM_PATH, encoding="utf-8-sig").reset_index(drop=True)
    coverage = pd.read_excel(COVERAGE_PATH, sheet_name="coverage_summary")

    require_columns(
        clients,
        ["program_expense_id", "client_expense_title",
         "client_expense_description", "client_expense_class"],
        CLIENT_PATH,
    )
    require_columns(programs, PROGRAM_TEXT_COLUMNS + ["expense_class"], PROGRAM_PATH)
    require_columns(coverage, ["expense_class", "k_95"], COVERAGE_PATH)

    if clients.empty or programs.empty:
        raise ValueError("Client and program input files must not be empty.")

    # Source row is the 1-based data row, excluding the CSV header.
    clients["source_row"] = clients.index + 1

    # Always use the original program CSV row + 1, before any filtering.
    programs["program_expense_id"] = programs.index + 1

    anchor_ids = pd.to_numeric(clients["program_expense_id"], errors="raise")

    if anchor_ids.isna().any() or (anchor_ids % 1 != 0).any():
        raise ValueError("Client anchor IDs must be integers.")

    clients["program_expense_id"] = anchor_ids.astype(int)

    assert clients["program_expense_id"].isin(programs["program_expense_id"]).all(), (
        "A client anchor is missing from the program CSV."
    )

    for column in ["client_expense_title", "client_expense_description", "client_expense_class"]:
        clients[column] = clients[column].map(clean_value)

    for column in PROGRAM_TEXT_COLUMNS + ["expense_class"]:
        programs[column] = programs[column].map(clean_value)

    clients["_class_key"] = clients["client_expense_class"].map(normalize_class)
    programs["_class_key"] = programs["expense_class"].map(normalize_class)
    programs["_text_key"] = programs.apply(program_text_key, axis=1)

    assert clients["_class_key"].ne("").all(), "Client expense class must not be empty."
    assert (
        clients["client_expense_title"].ne("")
        | clients["client_expense_description"].ne("")
    ).all(), "A client has no expense text."

    k95_by_class = {}

    for _, row in coverage.iterrows():
        class_key = normalize_class(row["expense_class"])

        if not class_key or pd.isna(row["k_95"]):
            continue

        number = pd.to_numeric(row["k_95"], errors="raise")

        if not np.isfinite(number) or number < 0 or number % 1 != 0:
            raise ValueError(f"Invalid k_95 for {row['expense_class']!r}: {number}")

        k95 = int(number)

        if class_key in k95_by_class and k95_by_class[class_key] != k95:
            raise ValueError(f"Conflicting k_95 values for {row['expense_class']!r}.")

        k95_by_class[class_key] = k95

    required_classes = set(clients["_class_key"]) - {NON_FUNDABLE_CLASS}
    missing_classes = required_classes - set(k95_by_class)

    if missing_classes:
        raise ValueError(f"Missing k_95 values for client classes: {sorted(missing_classes)}")

    return clients, programs, k95_by_class


# ============================================================
# Local E5 embeddings
# ============================================================

def encode_texts(clients, programs):

    from sentence_transformers import SentenceTransformer

    print("Loading local embedding model:", EMBEDDING_MODEL_NAME)

    model = SentenceTransformer(
        EMBEDDING_MODEL_NAME,
        device="cpu",
        local_files_only=True,
    )

    # Text builders already supply E5 query / passage prefixes.
    # Keep the model's native max_seq_length, as in the distance analysis.
    model.default_prompt_name = None

    client_embeddings = model.encode(
        clients.apply(build_client_text, axis=1).tolist(),
        batch_size=EMBEDDING_BATCH_SIZE,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=True,
        prompt="",
    )

    program_embeddings = model.encode(
        programs.apply(build_program_text, axis=1).tolist(),
        batch_size=EMBEDDING_BATCH_SIZE,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=True,
        prompt="",
    )

    assert client_embeddings.ndim == program_embeddings.ndim == 2
    assert client_embeddings.shape[1] == program_embeddings.shape[1] > 0

    for values in [client_embeddings, program_embeddings]:
        assert np.isfinite(values).all(), "Non-finite embeddings."
        assert np.allclose(np.linalg.norm(values, axis=1), 1.0, atol=1e-4)

    return client_embeddings, program_embeddings


# ============================================================
# Rank and select candidates
# ============================================================

def build_candidates(clients, programs, k95_by_class, client_embeddings,
                     program_embeddings, semi_hard_per_client):

    rows = []
    short_clients = []
    requested = semi_hard_per_client + 1

    valid_programs = (
        programs["_class_key"].ne("")
        & programs["_class_key"].ne(NON_FUNDABLE_CLASS)
        & (programs["category"].ne("") | programs["subcategory"].ne(""))
    )

    for client_index, client in clients.iterrows():
        source_row = int(client["source_row"])
        anchor_id = int(client["program_expense_id"])
        anchor = programs.iloc[anchor_id - 1]
        class_key = client["_class_key"]

        assert anchor["_class_key"] == class_key, (
            f"source_row={source_row}: client and anchor classes differ."
        )

        mask = (
            valid_programs
            & programs["_class_key"].eq(class_key)
            & programs["program_expense_id"].ne(anchor_id)
            & programs["_text_key"].map(lambda key: key != anchor["_text_key"])
        )

        indices = np.flatnonzero(mask.to_numpy())

        unique_count = programs.iloc[indices]["_text_key"].nunique()

        if unique_count < requested:
            short_clients.append(source_row)
            print(
                f"Warning: source_row={source_row}, class={client['client_expense_class']!r}: "
                f"{unique_count} distinct eligible texts ({len(indices)} program rows); "
                f"requested {requested}. Keeping available distinct candidates."
            )

        if not len(indices):
            continue

        k95 = k95_by_class[class_key]
        similarities = np.clip(program_embeddings[indices] @ client_embeddings[client_index], -1.0, 1.0)

        # Equal similarities are resolved by original program row / ID.
        order = np.argsort(-similarities, kind="stable")

        ranked_keys = [
            programs.iloc[int(indices[int(ordered_index)])]["_text_key"]
            for ordered_index in order
        ]

        selected_keys = set()
        selections = []

        for position, key in enumerate(ranked_keys):
            if len(selections) >= semi_hard_per_client:
                break
            if key in selected_keys:
                continue
            selections.append((position, "semi_hard_negative", ""))
            selected_keys.add(key)

        # Positions refer to the complete eligible ranking. Do not rerank
        # after reserving semi-hard candidates. Exclude all text twins
        # of selected candidates, even when their program IDs differ.
        remaining_positions = [
            position for position, key in enumerate(ranked_keys)
            if key not in selected_keys
        ]

        if remaining_positions:
            outside = [position for position in remaining_positions if position + 1 > k95]

            if outside:
                easy_position = outside[0]
                strategy = "outside_k95"
            else:
                easy_position = remaining_positions[-1]
                strategy = "farthest_fallback"

            selections.append((easy_position, "easy_negative", strategy))
            selected_keys.add(ranked_keys[easy_position])

        assert len(selected_keys) == len(selections), (
            f"source_row={source_row}: duplicate selected program text."
        )

        for position, category, strategy in selections:
            ordered_index = int(order[position])
            program_index = int(indices[ordered_index])
            program = programs.iloc[program_index]

            assert int(program["program_expense_id"]) != anchor_id
            assert program["_text_key"] != anchor["_text_key"]
            assert program["_class_key"] == class_key
            assert class_key != NON_FUNDABLE_CLASS

            rows.append({
                "pair_category": category,
                "source_row": source_row,
                "client_expense_title": client["client_expense_title"],
                "client_expense_description": client["client_expense_description"],
                "client_expense_class": client["client_expense_class"],
                "anchor_program_expense_id": anchor_id,
                "program_expense_id": int(program["program_expense_id"]),
                "program_title": clean_value(program.get("program_title", "")),
                "subprogram_name": clean_value(program.get("subprogram_name", "")),
                "program_category": program["category"],
                "program_subcategory": program["subcategory"],
                "program_expense_description": program["expense_description"],
                "program_expense_class": program["expense_class"],
                "cosine_similarity": float(similarities[ordered_index]),
                "rank": position + 1,
                "k95": k95,
                "easy_strategy": strategy,
                "generation_run": "v05_negatives",
            })

    candidates = pd.DataFrame(rows, columns=OUTPUT_COLUMNS[1:])

    candidates.insert(0, "candidate_id", [
        f"N{number:04d}" for number in range(1, len(candidates) + 1)
    ])

    assert candidates["candidate_id"].is_unique
    assert not candidates.duplicated(["source_row", "program_expense_id"]).any()
    assert not candidates.duplicated([
        "source_row", "program_category", "program_subcategory", "program_expense_description",
    ]).any(), "Duplicate program text for the same client."
    assert "label" not in candidates.columns

    return candidates, short_clients


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Build v0.5 negative candidates with offline E5 embeddings; no labels or API calls."
    )

    parser.add_argument("--semi-hard-per-client", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")

    args = parser.parse_args()

    if args.semi_hard_per_client < 0:
        parser.error("--semi-hard-per-client must be non-negative.")

    for path in [CSV_OUTPUT_PATH, EXCEL_OUTPUT_PATH]:
        if path.exists() and not args.overwrite and not args.dry_run:
            raise FileExistsError(f"{path} already exists. Use --overwrite to replace it.")

    clients, programs, k95_by_class = load_inputs()

    print("Client rows loaded (all will be processed):", len(clients))
    print("Program expenses loaded:", len(programs))

    client_embeddings, program_embeddings = encode_texts(clients, programs)

    candidates, short_clients = build_candidates(
        clients, programs, k95_by_class,
        client_embeddings, program_embeddings, args.semi_hard_per_client,
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print("=" * 70)
    print("NEGATIVE CANDIDATES V0.5 (UNLABELLED)")
    print("=" * 70)
    print("Clients processed:", len(clients))
    print("Candidates:", len(candidates))
    print()
    print("Per pair category:")
    print(candidates["pair_category"].value_counts().reindex(
        ["easy_negative", "semi_hard_negative"], fill_value=0,
    ).to_string())
    print()
    print("Per class and pair category:")
    print(candidates.groupby(["client_expense_class", "pair_category"]).size().to_string())
    print()
    print("Easy farthest_fallback:", int(candidates["easy_strategy"].eq("farthest_fallback").sum()))
    print("Clients with fewer candidates than requested:", len(short_clients))
    print("Their source_row values:", short_clients)

    if args.dry_run:
        print()
        print("Five examples:")
        print(candidates.head(5).to_string(index=False))
        print("Dry run complete: no output files written, no API calls.")
        return

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    csv_bytes = candidates.to_csv(index=False).encode("utf-8-sig")
    excel_buffer = io.BytesIO()

    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        candidates.to_excel(writer, sheet_name="candidates", index=False)
        sheet = writer.sheets["candidates"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

    OUTPUT_DATA_DIR.mkdir(parents=True, exist_ok=True)

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
    print("=" * 70)


if __name__ == "__main__":
    main()
