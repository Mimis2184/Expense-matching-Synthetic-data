import argparse
import io
import json
import sys
import unicodedata
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field


# ============================================================
# Project root
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

sys.path.insert(
    0,
    str(BASE_DIR),
)


# ============================================================
# Reuse existing utilities
# ============================================================

from config import (
    OUTPUT_DATA_DIR,
    PROCESSED_DATA_DIR,
)

from src.generator import (
    clean_json_response,
    create_llm,
    read_text_file,
)

from src.lexical_score import (
    lexical_score,
)


# ============================================================
# Non-lexical few-shot generation
#
# Input: the human-validated positive pairs with new_score = 0
# (no common words between client and program Category /
# Subcategory).
#
# The rows are grouped by Category + Subcategory. For each group
# the LLM sees the group's rows as examples and writes
# N_PER_EXAMPLE new client expenses per example. Every new client
# keeps the row number of the example it came from (source_row).
#
# Modes:
#   dry_run  build the prompts and save them to a file,
#            NO API call (default)
#   pilot    one group per expense class
#   full     every group
#   full_v05 linked program expenses, grouped by their full text;
#            --dry-run previews this mode without writing files
#
# No validator is called. The new pairs are scored locally with
# new_score and reviewed manually.
# ============================================================

INPUT_PATH = (
    PROCESSED_DATA_DIR
    / "direct-expense-match-scores-2026-10-05_new_score.xlsx"
)

SYSTEM_PROMPT_PATH = (
    BASE_DIR
    / "prompts"
    / "non_lexical_system_prompt.md"
)

GENERATION_PROMPT_PATH = (
    BASE_DIR
    / "prompts"
    / "non_lexical_generation_prompt.md"
)

DRY_RUN_PROMPTS_PATH = (
    OUTPUT_DATA_DIR
    / "non_lexical_prompts_dry_run.md"
)

N_PER_EXAMPLE = 3

MAX_ROWS_PER_CALL = 10

V05_SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "non_lexical_system_prompt_v05.md"
V05_GENERATION_PROMPT_PATH = BASE_DIR / "prompts" / "non_lexical_generation_prompt_v05.md"
V05_EXCEL_PATH = OUTPUT_DATA_DIR / "non_lexical_pairs_v05.xlsx"
V05_CSV_PATH = OUTPUT_DATA_DIR / "non_lexical_pairs_v05.csv"

V05_PAIR_COLUMNS = [
    "source_row", "source_client_expense", "source_client_expense_description",
    "client_expense_title", "client_expense_description", "client_expense_class",
    "program_category", "program_subcategory", "program_expense_class",
    "new_score", "manual_review", "program_expense_id", "program_title",
    "subprogram_name", "program_expense_description", "generation_run",
]

TEMPERATURE = 0.8

# Excel row of the first data row (row 1 is the header).
FIRST_EXCEL_ROW = 2

COLUMNS = {
    "title": "Client Expense",
    "description": "Client Expense Description",
    "client_class": "Client Expense Class",
    "category": "Ideal Expense Category",
    "subcategory": "Ideal Expense Subcategory",
    "program_class": "Ideal Expense Class",
    "score": "new_score",
}

DASHES = {
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "−": "-",
}


# ============================================================
# LLM output models
# ============================================================

class NonLexicalClientExpense(BaseModel):

    source_row: int

    expense_title: str = Field(
        min_length=1
    )

    expense_description: str = Field(
        min_length=1
    )

    expense_class: str = Field(
        min_length=1
    )


class NonLexicalClientBatch(BaseModel):

    expenses: list[NonLexicalClientExpense]


# ============================================================
# Helpers
# ============================================================

def clean_value(value) -> str:

    if pd.isna(value):
        return ""

    return str(value).strip()


def group_key(value) -> str:
    """
    Comparison key: ignores case, accents, spaces and the kind
    of dash, so that e.g. "Πνευματική ιδιοκτησία – ..." and
    "Πνευματική ιδιοκτησία - ..." form one group.
    """

    text = clean_value(value).casefold()

    for dash, plain in DASHES.items():
        text = text.replace(dash, plain)

    text = "".join(
        character
        for character in unicodedata.normalize("NFD", text)
        if unicodedata.category(character) != "Mn"
    )

    return " ".join(text.split())


# ============================================================
# Load and group the non-lexical examples
# ============================================================

def load_examples() -> pd.DataFrame:

    df = pd.read_excel(
        INPUT_PATH,
    )

    missing_columns = [
        column
        for column in COLUMNS.values()
        if column not in df.columns
    ]

    if missing_columns:
        raise ValueError(
            "Missing required columns: "
            f"{missing_columns}"
        )

    # Row number as seen in the Excel file.
    df["source_row"] = (
        df.index + FIRST_EXCEL_ROW
    )

    examples = df[
        df[COLUMNS["score"]] == 0
    ].copy()

    examples["group"] = (
        examples[COLUMNS["client_class"]].map(group_key)
        + " || "
        + examples[COLUMNS["category"]].map(group_key)
        + " || "
        + examples[COLUMNS["subcategory"]].map(group_key)
    )

    # --------------------------------------------------------
    # Drop examples with the same client title inside a group
    # --------------------------------------------------------

    examples["_title_key"] = (
        examples[COLUMNS["title"]].map(group_key)
    )

    examples = examples.drop_duplicates(
        subset=["group", "_title_key"],
        keep="first",
    )

    return examples


def build_chunks(
    examples: pd.DataFrame,
) -> list[pd.DataFrame]:
    """
    One chunk = one LLM call: the rows of one group,
    at most MAX_ROWS_PER_CALL.
    """

    chunks = []

    group_sizes = (
        examples["group"]
        .value_counts()
    )

    for group in group_sizes.index:

        group_df = examples[
            examples["group"] == group
        ]

        for start in range(
            0,
            len(group_df),
            MAX_ROWS_PER_CALL,
        ):
            chunks.append(
                group_df.iloc[
                    start:start + MAX_ROWS_PER_CALL
                ]
            )

    return chunks


def select_pilot_chunks(
    chunks: list[pd.DataFrame],
) -> list[pd.DataFrame]:
    """
    One chunk per expense class: the first chunk of the class
    group with the most examples. build_chunks orders groups
    from largest to smallest, so the first chunk seen for a
    class is that one.
    """

    selected = []

    seen_classes = set()

    for chunk in chunks:

        class_key = group_key(
            chunk[COLUMNS["client_class"]].iloc[0]
        )

        if class_key in seen_classes:
            continue

        seen_classes.add(class_key)

        selected.append(chunk)

    return selected


# ============================================================
# Prompt
# ============================================================

def format_examples(
    chunk: pd.DataFrame,
) -> str:

    lines = []

    for _, row in chunk.iterrows():

        lines.append(
            f"[row {row['source_row']}] "
            f"Client: {clean_value(row[COLUMNS['title']])} — "
            f"{clean_value(row[COLUMNS['description']])}"
        )

    return "\n".join(lines)


def build_prompt(
    chunk: pd.DataFrame,
    system_prompt: str,
    generation_template: str,
) -> tuple[str, str]:

    first = chunk.iloc[0]

    generation_prompt = (
        generation_template
        .replace(
            "{expense_class}",
            clean_value(first[COLUMNS["client_class"]]),
        )
        .replace(
            "{category}",
            clean_value(first[COLUMNS["category"]]),
        )
        .replace(
            "{subcategory}",
            clean_value(first[COLUMNS["subcategory"]]),
        )
        .replace(
            "{few_shot_examples}",
            format_examples(chunk),
        )
        .replace(
            "{n_per_example}",
            str(N_PER_EXAMPLE),
        )
    )

    return (
        system_prompt,
        generation_prompt,
    )


# ============================================================
# Generation for one chunk
# ============================================================

def generate_for_chunk(
    llm,
    chunk: pd.DataFrame,
    system_prompt: str,
    generation_prompt: str,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (pairs, issues). One failed chunk never stops the run.
    """

    pairs = []

    issues = []

    first = chunk.iloc[0]

    expected_class = clean_value(
        first[COLUMNS["client_class"]]
    )

    examples_by_row = {
        int(row["source_row"]): row
        for _, row in chunk.iterrows()
    }

    try:

        response = llm.invoke(
            [
                ("system", system_prompt),
                ("human", generation_prompt),
            ]
        )

        data = json.loads(
            clean_json_response(
                str(response.content)
            )
        )

        batch = NonLexicalClientBatch.model_validate(
            data
        )

    except Exception as exc:

        issues.append(
            {
                "source_rows": ", ".join(
                    str(row) for row in examples_by_row
                ),
                "issue": f"Generation failed: {exc}",
            }
        )

        return pairs, issues

    generated_per_row = {
        row: 0
        for row in examples_by_row
    }

    for expense in batch.expenses:

        # ----------------------------------------------------
        # Checks on every generated client
        # ----------------------------------------------------

        if expense.source_row not in examples_by_row:

            issues.append(
                {
                    "source_rows": str(expense.source_row),
                    "issue": (
                        "Unknown source_row: "
                        f"{expense.expense_title}"
                    ),
                }
            )

            continue

        if (
            group_key(expense.expense_class)
            != group_key(expected_class)
        ):

            issues.append(
                {
                    "source_rows": str(expense.source_row),
                    "issue": (
                        "Wrong expense_class: "
                        f"{expense.expense_class}"
                    ),
                }
            )

            continue

        title = expense.expense_title.strip()

        description = expense.expense_description.strip()

        if not title or not description:

            issues.append(
                {
                    "source_rows": str(expense.source_row),
                    "issue": "Empty title or description",
                }
            )

            continue

        example = examples_by_row[
            expense.source_row
        ]

        category = clean_value(
            example[COLUMNS["category"]]
        )

        subcategory = clean_value(
            example[COLUMNS["subcategory"]]
        )

        pairs.append(
            {
                "source_row":
                    expense.source_row,

                "source_client_expense":
                    clean_value(example[COLUMNS["title"]]),

                "source_client_expense_description":
                    clean_value(example[COLUMNS["description"]]),

                "client_expense_title":
                    title,

                "client_expense_description":
                    description,

                "client_expense_class":
                    expected_class,

                "program_category":
                    category,

                "program_subcategory":
                    subcategory,

                "program_expense_class":
                    clean_value(example[COLUMNS["program_class"]]),

                "new_score":
                    round(
                        lexical_score(
                            title,
                            description,
                            category,
                            subcategory,
                        ),
                        4,
                    ),

                "manual_review":
                    "",
            }
        )

        generated_per_row[expense.source_row] += 1

    # --------------------------------------------------------
    # Examples that did not get N_PER_EXAMPLE clients
    # --------------------------------------------------------

    for row, count in generated_per_row.items():

        if count != N_PER_EXAMPLE:

            issues.append(
                {
                    "source_rows": str(row),
                    "issue": (
                        f"Expected {N_PER_EXAMPLE} clients, "
                        f"received {count}"
                    ),
                }
            )

    return pairs, issues


# ============================================================
# Full v0.5: resolve anchors before generation
# ============================================================

def link_examples_v05(examples: pd.DataFrame, program_df: pd.DataFrame):
    """
    Reuse the annotation resolver, with strict uniqueness across
    matching human rows. Missing descriptions do not exclude a row.
    """

    from src.build_annotation_sample import (
        load_human_pairs,
        match_example_program_expense,
    )

    human = load_human_pairs()
    linked_rows = []
    issues = []

    for _, example in examples.iterrows():

        program = match_example_program_expense(
            example,
            human,
            program_df,
            require_unique_human_program=True,
        )

        if program is None:
            issues.append({
                "source_rows": str(int(example["source_row"])),
                "issue": "Excluded before generation: no unique linked program expense.",
            })
            continue

        program_class = clean_value(program["expense_class"])

        if not program_class or group_key(example[COLUMNS["client_class"]]) != group_key(program_class):
            issues.append({
                "source_rows": str(int(example["source_row"])),
                "issue": "Excluded before generation: example client and linked program classes differ or are empty.",
            })
            continue

        linked = example.to_dict()
        linked[COLUMNS["client_class"]] = program_class
        linked[COLUMNS["program_class"]] = program_class
        linked[COLUMNS["category"]] = clean_value(program["category"])
        linked[COLUMNS["subcategory"]] = clean_value(program["subcategory"])
        linked["program_expense_id"] = int(program["program_expense_id"])
        linked["program_title"] = clean_value(program.get("program_title", ""))
        linked["subprogram_name"] = clean_value(program.get("subprogram_name", ""))
        linked["program_expense_description"] = clean_value(program["expense_description"])

        # Exact stripped fields: two different descriptions never share
        # a prompt, even if their categories are otherwise identical.
        linked["group"] = json.dumps([
            program_class,
            linked[COLUMNS["category"]],
            linked[COLUMNS["subcategory"]],
            linked["program_expense_description"],
        ], ensure_ascii=False)

        linked_rows.append(linked)

    extra_columns = [
        "program_expense_id", "program_title", "subprogram_name", "program_expense_description",
    ]
    linked_df = pd.DataFrame(linked_rows, columns=list(examples.columns) + extra_columns)

    return linked_df, issues


def run_full_v05(args) -> None:
    """
    Separate orchestration so the original modes keep their behavior.
    Use the existing example filtering, chunking and client validation.
    """

    from src.build_evaluation_set import load_program_expenses

    for path in [V05_EXCEL_PATH, V05_CSV_PATH]:
        if path.exists() and not args.overwrite and not args.dry_run:
            raise FileExistsError(f"{path} already exists. Use --overwrite to replace it.")

    examples = load_examples()
    program_df = load_program_expenses()
    linked, all_issues = link_examples_v05(examples, program_df)
    chunks = build_chunks(linked)

    excluded_rows = [issue["source_rows"] for issue in all_issues]
    no_description = int(linked["program_expense_description"].eq("").sum())

    print("=" * 70)
    print("NON-LEXICAL FEW-SHOT GENERATION V0.5")
    print("=" * 70)
    print("Non-lexical examples (after duplicate titles):", len(examples))
    print("Linked examples:", len(linked))
    print("Excluded examples:", len(excluded_rows))
    print("Excluded source_row values:", ", ".join(excluded_rows) or "none")
    print("Linked examples without program description:", no_description)
    print("Groups:", linked["group"].nunique())
    print("Planned calls:", len(chunks))
    print("Clients expected:", len(linked) * N_PER_EXAMPLE)

    system_prompt = read_text_file(V05_SYSTEM_PROMPT_PATH)
    generation_template = read_text_file(V05_GENERATION_PROMPT_PATH)
    prompts = []

    for chunk in chunks:
        description = clean_value(chunk["program_expense_description"].iloc[0])
        # Fill the new field before the existing builder inserts examples.
        template = generation_template.replace(
            "{program_expense_description}", description or "(no description)",
        )
        prompts.append(build_prompt(chunk, system_prompt, template))

    if args.dry_run:
        if prompts:
            print()
            print("FULL SYSTEM PROMPT (first call)")
            print(prompts[0][0])
            print()
            print("FULL HUMAN PROMPT (first call)")
            print(prompts[0][1])
        else:
            print("No linked examples; no call prompt is available.")
        characters = sum(len(system) + len(human) for system, human in prompts)
        print("Total input prompt characters:", characters)
        print(f"Estimated input tokens (characters / 4): {characters / 4:,.0f}")
        print("Calls made: 0. No output files written.")
        print("=" * 70)
        return

    # Create an LLM only when there is actual generation to perform.
    llm = create_llm(temperature=TEMPERATURE) if chunks else None
    all_pairs = []

    for number, (chunk, (system_text, human_text)) in enumerate(zip(chunks, prompts), start=1):

        print(f"Call {number}/{len(chunks)} ({len(chunk)} examples)")

        pairs, issues = generate_for_chunk(llm, chunk, system_text, human_text)

        examples_by_row = {
            int(row["source_row"]): row for _, row in chunk.iterrows()
        }

        for pair in pairs:
            example = examples_by_row[int(pair["source_row"])]
            pair.update({
                "program_expense_id": int(example["program_expense_id"]),
                "program_title": example["program_title"],
                "subprogram_name": example["subprogram_name"],
                "program_expense_description": example["program_expense_description"],
                "generation_run": "v05_non_lexical",
            })

        all_pairs.extend(pairs)
        all_issues.extend(issues)

    pairs_df = pd.DataFrame(all_pairs, columns=V05_PAIR_COLUMNS)
    issues_df = pd.DataFrame(all_issues, columns=["source_rows", "issue"])

    if not pairs_df.empty:
        pairs_df = pairs_df.sort_values("source_row", kind="mergesort").reset_index(drop=True)

        assert pairs_df["program_expense_id"].isin(program_df["program_expense_id"]).all()
        classes_by_id = program_df.set_index("program_expense_id")["expense_class"].map(group_key)
        linked_classes = pairs_df["program_expense_id"].map(classes_by_id)
        assert pairs_df["program_expense_class"].map(group_key).eq(linked_classes).all()
        assert pairs_df["client_expense_class"].map(group_key).eq(linked_classes).all()

    assert "label" not in pairs_df.columns

    # Prepare both outputs, then protect both destinations before saving.
    csv_bytes = pairs_df.to_csv(index=False).encode("utf-8-sig")
    excel_buffer = io.BytesIO()

    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        pairs_df.to_excel(writer, sheet_name="pairs", index=False)
        issues_df.to_excel(writer, sheet_name="issues", index=False)

    OUTPUT_DATA_DIR.mkdir(parents=True, exist_ok=True)

    for path in [V05_EXCEL_PATH, V05_CSV_PATH]:
        if path.exists() and not args.overwrite:
            raise FileExistsError(f"{path} already exists. Use --overwrite to replace it.")

    mode = "wb" if args.overwrite else "xb"

    with V05_EXCEL_PATH.open(mode) as handle:
        handle.write(excel_buffer.getvalue())

    with V05_CSV_PATH.open(mode) as handle:
        handle.write(csv_bytes)

    print()
    print("Calls made:", len(chunks))
    print("Clients generated:", len(pairs_df))
    print("Clients with new_score = 0:", int(pairs_df["new_score"].eq(0).sum()))
    print("Issues:", len(issues_df))
    if not issues_df.empty:
        print(issues_df.to_string(index=False))
    print("Saved:", V05_EXCEL_PATH)
    print("Saved:", V05_CSV_PATH)
    print("=" * 70)


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Few-shot generation of non-lexical positive pairs."
        )
    )

    parser.add_argument(
        "--mode",
        choices=["dry_run", "pilot", "full", "full_v05"],
        default="dry_run",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing output Excel.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview full_v05 prompts and counts without API calls or output files.",
    )

    args = parser.parse_args()

    if args.dry_run and args.mode != "full_v05":
        parser.error("--dry-run is supported with --mode full_v05; use --mode dry_run for the original preview.")

    if args.mode == "full_v05":
        run_full_v05(args)
        return

    # --------------------------------------------------------
    # Examples and calls
    # --------------------------------------------------------

    examples = load_examples()

    chunks = build_chunks(
        examples
    )

    pilot_chunks = select_pilot_chunks(
        chunks
    )

    selected_chunks = (
        chunks
        if args.mode == "full"
        else pilot_chunks
    )

    print("=" * 70)
    print("NON-LEXICAL FEW-SHOT GENERATION")
    print("=" * 70)
    print("Mode:", args.mode)
    print("Non-lexical examples (after duplicate titles):", len(examples))
    print("Groups:", examples["group"].nunique())
    print("Calls for pilot:", len(pilot_chunks))
    print("Calls for full:", len(chunks))
    print("Calls in this run:", 0 if args.mode == "dry_run" else len(selected_chunks))
    print(
        "Clients expected in this run:",
        sum(len(chunk) for chunk in selected_chunks) * N_PER_EXAMPLE,
    )

    system_prompt = read_text_file(
        SYSTEM_PROMPT_PATH
    )

    generation_template = read_text_file(
        GENERATION_PROMPT_PATH
    )

    prompts = [
        build_prompt(
            chunk,
            system_prompt,
            generation_template,
        )
        for chunk in selected_chunks
    ]

    # --------------------------------------------------------
    # dry_run: save the prompts, no API call
    # --------------------------------------------------------

    if args.mode == "dry_run":

        DRY_RUN_PROMPTS_PATH.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        sections = []

        for number, (system_text, human_text) in enumerate(
            prompts,
            start=1,
        ):
            sections.append(
                f"# Call {number} of {len(prompts)}\n\n"
                f"## System\n\n{system_text}\n\n"
                f"## Human\n\n{human_text}\n"
            )

        DRY_RUN_PROMPTS_PATH.write_text(
            "\n\n".join(sections),
            encoding="utf-8",
        )

        print()
        print("No API call was made.")
        print("Pilot prompts saved:", DRY_RUN_PROMPTS_PATH)
        print("=" * 70)

        return

    # --------------------------------------------------------
    # pilot / full: never overwrite reviewed results by mistake
    # --------------------------------------------------------

    output_path = (
        OUTPUT_DATA_DIR
        / f"non_lexical_pairs_{args.mode}.xlsx"
    )

    if output_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"{output_path} already exists. "
            "Use --overwrite to replace it."
        )

    llm = create_llm(
        temperature=TEMPERATURE
    )

    all_pairs = []

    all_issues = []

    for number, (chunk, (system_text, human_text)) in enumerate(
        zip(selected_chunks, prompts),
        start=1,
    ):

        print(
            f"Call {number}/{len(selected_chunks)}: "
            f"{clean_value(chunk[COLUMNS['subcategory']].iloc[0])[:60]} "
            f"({len(chunk)} examples)"
        )

        pairs, issues = generate_for_chunk(
            llm,
            chunk,
            system_text,
            human_text,
        )

        all_pairs.extend(pairs)

        all_issues.extend(issues)

    pairs_df = pd.DataFrame(
        all_pairs
    )

    issues_df = pd.DataFrame(
        all_issues,
        columns=["source_rows", "issue"],
    )

    if not pairs_df.empty:
        pairs_df = (
            pairs_df
            .sort_values(
                "source_row",
                kind="mergesort",
            )
            .reset_index(drop=True)
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with pd.ExcelWriter(
        output_path,
        engine="openpyxl",
    ) as writer:

        pairs_df.to_excel(
            writer,
            sheet_name="pairs",
            index=False,
        )

        issues_df.to_excel(
            writer,
            sheet_name="issues",
            index=False,
        )

    zero_score = (
        int((pairs_df["new_score"] == 0).sum())
        if not pairs_df.empty
        else 0
    )

    print()
    print("Calls made:", len(selected_chunks))
    print("Clients generated:", len(pairs_df))
    print("Clients with new_score = 0:", zero_score)
    print("Issues:", len(issues_df))
    print("Saved:", output_path)
    print("=" * 70)


if __name__ == "__main__":
    main()
