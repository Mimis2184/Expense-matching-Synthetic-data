import sys
from pathlib import Path

import pandas as pd


sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    CLEAN_CLIENT_DATA_PATH,
    SEEDS_PATH,
    N_SEEDS_PER_GENERATION,
)

from style_analysis import (
    detect_language_style,
    is_uppercase_text,
    has_digit,
)


RANDOM_SEED = 42


def calculate_allocation(
    group: pd.DataFrame,
    n_seeds: int,
) -> dict[str, int]:
    """
    Υπολογίζει πόσα seeds θα πάρουμε
    από κάθε writing-style group.
    """

    counts = group["seed_stratum"].value_counts()

    total = len(group)

    exact_allocation = {}
    allocation = {}

    for stratum, count in counts.items():

        exact = n_seeds * count / total

        exact_allocation[stratum] = exact

        allocation[stratum] = int(exact)

    remaining = n_seeds - sum(allocation.values())

    remainders = sorted(
        exact_allocation.keys(),
        key=lambda stratum: (
            exact_allocation[stratum]
            - allocation[stratum]
        ),
        reverse=True,
    )

    for stratum in remainders[:remaining]:
        allocation[stratum] += 1

    return allocation


def select_seeds_for_class(
    group: pd.DataFrame,
    n_seeds: int,
    random_seed: int,
) -> pd.DataFrame:
    """
    Επιλέγει αντιπροσωπευτικά seeds
    από μία expense_class.
    """

    group = group.copy()

    n_seeds = min(
        n_seeds,
        len(group),
    )

    allocation = calculate_allocation(
        group,
        n_seeds,
    )

    selected_parts = []

    for i, (stratum, number_to_select) in enumerate(
        allocation.items()
    ):

        if number_to_select == 0:
            continue

        candidates = group[
            group["seed_stratum"] == stratum
        ]

        selected = candidates.sample(
            n=number_to_select,
            random_state=random_seed + i,
        )

        selected_parts.append(selected)

    selected = pd.concat(
        selected_parts,
        axis=0,
    )

    # Αν για οποιονδήποτε λόγο λείπουν seeds,
    # συμπληρώνουμε από τα υπόλοιπα examples.
    if len(selected) < n_seeds:

        remaining_needed = (
            n_seeds - len(selected)
        )

        remaining_pool = group.drop(
            index=selected.index
        )

        extra = remaining_pool.sample(
            n=remaining_needed,
            random_state=random_seed,
        )

        selected = pd.concat(
            [selected, extra],
            axis=0,
        )

    selected = selected.sample(
        frac=1,
        random_state=random_seed,
    )

    selected = selected.reset_index(drop=True)

    selected["seed_order"] = (
        range(1, len(selected) + 1)
    )

    return selected


def create_seed_dataset() -> None:

    print(f"Reading: {CLEAN_CLIENT_DATA_PATH}")

    df = pd.read_csv(
        CLEAN_CLIENT_DATA_PATH,
        encoding="utf-8-sig",
    )

    print(f"Rows loaded: {len(df):,}")

    # ---------------------------------------------------
    # Δημιουργία χαρακτηριστικών για κάθε example
    # ---------------------------------------------------

    df["title_equals_description"] = (
        df["expense_title"].str.strip()
        == df["expense_description"].str.strip()
    )

    df["language_style"] = (
        df["expense_title"]
        .apply(detect_language_style)
    )

    df["title_uppercase"] = (
        df["expense_title"]
        .apply(is_uppercase_text)
    )

    df["title_has_digit"] = (
        df["expense_title"]
        .apply(has_digit)
    )

    # Συνδυάζουμε language style
    # και title=description σε ένα stratum.
    df["seed_stratum"] = (
        df["language_style"]
        + "_"
        + df["title_equals_description"]
        .map({
            True: "same",
            False: "different",
        })
    )

    # ---------------------------------------------------
    # Seed selection ανά expense_class
    # ---------------------------------------------------

    all_selected_seeds = []

    class_order = (
        df["expense_class"]
        .value_counts()
        .index
    )

    for class_number, expense_class in enumerate(
        class_order
    ):

        group = df[
            df["expense_class"] == expense_class
        ].copy()

        selected = select_seeds_for_class(
            group=group,
            n_seeds=N_SEEDS_PER_GENERATION,
            random_seed=RANDOM_SEED + class_number,
        )

        all_selected_seeds.append(selected)

    # Ενώνουμε όλα τα selected seeds
    seeds_df = pd.concat(
        all_selected_seeds,
        ignore_index=True,
    )

    # ---------------------------------------------------
    # Κρατάμε τις χρήσιμες στήλες
    # ---------------------------------------------------

    seeds_df = seeds_df[
        [
            "expense_class",
            "seed_order",
            "expense_title",
            "expense_description",
            "language_style",
            "title_equals_description",
            "title_uppercase",
            "title_has_digit",
        ]
    ]

    # ---------------------------------------------------
    # Αποθήκευση
    # ---------------------------------------------------

    SEEDS_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    seeds_df.to_csv(
        SEEDS_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    print()
    print(f"Saved: {SEEDS_PATH}")
    print(f"Total selected seeds: {len(seeds_df):,}")

    print()
    print("Seeds per expense_class:")

    print(
        seeds_df[
            "expense_class"
        ]
        .value_counts()
        .to_string()
    )


if __name__ == "__main__":
    create_seed_dataset()