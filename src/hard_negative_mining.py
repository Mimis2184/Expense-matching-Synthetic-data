import random
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


# ============================================================
# Reuse the validated easy-negative logic
# ============================================================

from src.generator import create_llm

from src.easy_negative_candidate_selection import (
    DISTANCE_ANALYSIS_PATH,
    CHECKPOINT_NEGATIVES_PATH,
    assert_non_fundable_detection,
    clean_value,
    detect_non_fundable_pair,
    normalize_class,
    normalize_marker_value,
    program_text_key,
    static_validate_pair,
    to_bool,
    to_int,
    validate_pair_with_retry,
)

from src.llm_verdict_cache import (
    VerdictCache,
    compute_pair_fingerprint,
    compute_prompt_hash,
)


# ============================================================
# Paths
# ============================================================

PILOT_REPORT_PATH = (
    BASE_DIR
    / "data/output/hard_negative_pilot_report.xlsx"
)

HARD_NEGATIVES_CSV_PATH = (
    BASE_DIR
    / "data/output/hard_negative_pairs_validated.csv"
)

HARD_NEGATIVES_EXCEL_PATH = (
    BASE_DIR
    / "data/output/hard_negative_pairs_validated.xlsx"
)


# ============================================================
# PILOT SETTINGS
#
# Semi-hard candidates: same class, not the known positive,
# just BELOW the known positive similarity:
#
#     margin = positive_similarity - candidate_similarity
#     margin > MIN_MARGIN, smallest margin first
#
# At most MAX_CANDIDATES_PER_CLIENT validations per client.
# The search stops at the first INVALID and is never extended.
# ============================================================

PILOT_SEED = 42

MAX_CANDIDATES_PER_CLIENT = 3

MIN_MARGIN = 1e-4

SOURCE = "hard_pilot"

MINING_STRATEGY = "semi_hard"

# Class names exactly as found in embedding_distance_analysis.csv.
# They are matched accent/case/whitespace-insensitively, and every
# class in the data must belong to exactly one group.

MAIN_CLASSES = [
    "Άυλα αγορά, Άυλα χρήση (αποσβέσεις/συνδρομές)",
    "Γήπεδα χρήση (αποσβέσεις/μισθώσεις), Διαμόρφωση γηπέδων",
    "Δαπάνες προώθησης και επικοινωνίας (Marketing)",
    "Εξοπλισμός αγορά/κατασκευή, Εξοπλισμός χρήση (αποσβέσεις/μισθώσεις)",
    "Κεφάλαιο κίνησης (δαπάνες λειτουργίας, δαπάνες σχετικές με το "
    "συναλλακτικό κύκλωμα της επιχείρησης, ΦΠΑ, κ.λπ.)",
    "Κτήρια αγορά / κατασκευή, Κτήρια χρήση (αποσβέσεις/μισθώσεις)",
    "Μεταφορικά μέσα αγορά, μεταφορικά μέσα χρήση (αποσβέσεις/μισθώσεις)",
    "Μισθοδοσία συνδεδεμένη με το επενδυτικό σχέδιο "
    "(όπως προβλέπεται από τον ΓΑΚ)",
    "Υπηρεσίες τρίτων",
]

CONTROL_CLASSES = [
    "Αναλώσιμα",
    "Γήπεδα αγορά",
    "Λειτουργικά (επικοινωνία, ενέργεια, συντήρηση, μισθώματα, "
    "έξοδα διοίκησης, ασφάλιση κ.λπ..)",
    "Μετακινήσεις / εξοδολόγια",
]

EXCLUDED_CLASSES = [
    "Μη επιλέξιμες δαπάνες",
]

CLIENTS_PER_GROUP = {
    "main": 5,
    "control": 2,
}

MARGIN_BUCKET_EDGES = [
    MIN_MARGIN,
    0.0025,
    0.005,
    0.01,
    0.02,
    float("inf"),
]


# ============================================================
# Helpers
# ============================================================

def resolve_class_groups(
    actual_classes: list[str],
) -> dict[str, str]:

    """
    Map every actual expense_class to 'main' / 'control' / 'excluded'.
    Fails if a configured name matches nothing or several classes,
    or if an actual class is not configured.
    """

    actual_by_key: dict[str, list[str]] = {}

    for actual in actual_classes:
        actual_by_key.setdefault(
            normalize_marker_value(actual),
            [],
        ).append(actual)

    group_by_class = {}

    for group, configured_classes in [
        ("main", MAIN_CLASSES),
        ("control", CONTROL_CLASSES),
        ("excluded", EXCLUDED_CLASSES),
    ]:

        for configured in configured_classes:

            matches = actual_by_key.get(
                normalize_marker_value(configured),
                [],
            )

            if len(matches) != 1:
                raise ValueError(
                    f"Configured class {configured!r} matched "
                    f"{len(matches)} actual classes: {matches}"
                )

            if matches[0] in group_by_class:
                raise ValueError(
                    f"Class configured twice: {matches[0]!r}"
                )

            group_by_class[matches[0]] = group

    unassigned = sorted(set(actual_classes) - set(group_by_class))

    if unassigned:
        raise ValueError(
            f"Classes not assigned to any pilot group: {unassigned}"
        )

    return group_by_class


def sample_pilot_clients(
    pairs_df: pd.DataFrame,
    group_by_class: dict[str, str],
) -> pd.DataFrame:

    """
    Deterministic sampling with a fixed seed per class.
    """

    rows = []

    for expense_class, group in sorted(group_by_class.items()):

        if group == "excluded":
            continue

        client_ids = sorted(
            pairs_df.loc[
                pairs_df["expense_class"] == expense_class,
                "analysis_client_id",
            ]
            .astype(int)
            .unique()
            .tolist()
        )

        rng = random.Random(f"{PILOT_SEED}|{expense_class}")

        sample_size = min(
            CLIENTS_PER_GROUP[group],
            len(client_ids),
        )

        for client_id in sorted(rng.sample(client_ids, sample_size)):
            rows.append(
                {
                    "analysis_client_id": client_id,
                    "expense_class": expense_class,
                    "pilot_group": group,
                    "class_clients": len(client_ids),
                }
            )

    return pd.DataFrame(rows)


def load_easy_negative_keys() -> set[tuple[int, int | str]]:

    """
    Keys of the confirmed easy negatives, both by id and by text:

    - (analysis_client_id, program_expense_id)
    - (analysis_client_id, program_text_key)

    Different program ids can carry identical texts, so an id-only
    check would let a textual duplicate of an easy negative through.
    """

    easy_df = pd.read_csv(
        CHECKPOINT_NEGATIVES_PATH,
        encoding="utf-8-sig",
    )

    keys: set[tuple[int, int | str]] = set()

    for _, row in easy_df.iterrows():

        analysis_client_id = to_int(row["analysis_client_id"])

        keys.add(
            (
                analysis_client_id,
                to_int(row["candidate_program_expense_id"]),
            )
        )

        keys.add(
            (
                analysis_client_id,
                program_text_key(
                    row["program_category"],
                    row["program_subcategory"],
                    row["program_expense_description"],
                ),
            )
        )

    return keys


# ============================================================
# Semi-hard candidate selection
# ============================================================

def select_semi_hard_candidates(
    client_df: pd.DataFrame,
    easy_negative_keys: set[tuple[int, int | str]],
) -> tuple[pd.DataFrame, dict]:

    """
    Return ALL eligible semi-hard candidates of one client, ordered by
    ascending margin then ascending distance_rank. The validation loop
    takes at most MAX_CANDIDATES_PER_CLIENT of them (non-fundable skips
    do not use up a slot).
    """

    analysis_client_id = to_int(
        client_df["analysis_client_id"].iloc[0]
    )

    # --------------------------------------------------------
    # Known positive
    # --------------------------------------------------------

    positive_df = client_df[client_df["is_known_positive"]]

    if len(positive_df) != 1:
        raise ValueError(
            f"Client {analysis_client_id} has {len(positive_df)} "
            "known positives; expected exactly 1."
        )

    positive = positive_df.iloc[0]

    positive_id = to_int(positive["candidate_program_expense_id"])

    if positive_id != to_int(positive["positive_program_expense_id"]):
        raise ValueError(
            f"Client {analysis_client_id}: known positive row id "
            "does not match positive_program_expense_id."
        )

    positive_similarity = float(positive["cosine_similarity"])

    positive_text = program_text_key(
        positive["program_category"],
        positive["program_subcategory"],
        positive["program_expense_description"],
    )

    # --------------------------------------------------------
    # Same class (hard constraint) and not the known positive
    # --------------------------------------------------------

    class_key = normalize_class(positive["expense_class"])

    candidates = client_df[
        (client_df["expense_class"].apply(normalize_class) == class_key)
        & ~client_df["is_known_positive"]
        & (client_df["candidate_program_expense_id"] != positive_id)
    ].copy()

    total_non_positive = len(candidates)

    candidates["margin"] = (
        positive_similarity
        - candidates["cosine_similarity"].astype(float)
    )

    # --------------------------------------------------------
    # Exclusions
    # --------------------------------------------------------

    candidates["_program_text"] = [
        program_text_key(
            row["program_category"],
            row["program_subcategory"],
            row["program_expense_description"],
        )
        for _, row in candidates.iterrows()
    ]

    is_easy_negative = pd.Series(
        [
            (analysis_client_id, to_int(program_id)) in easy_negative_keys
            or (analysis_client_id, program_text) in easy_negative_keys
            for program_id, program_text in zip(
                candidates["candidate_program_expense_id"],
                candidates["_program_text"],
            )
        ],
        index=candidates.index,
        dtype=bool,
    )

    is_positive_duplicate = candidates["_program_text"] == positive_text

    above_min_margin = candidates["margin"] > MIN_MARGIN

    eligible = (
        candidates[
            ~is_easy_negative
            & ~is_positive_duplicate
            & above_min_margin
        ]
        .sort_values(
            ["margin", "distance_rank"],
            ascending=[True, True],
        )
    )

    # Several program ids can carry identical texts: the validator
    # would see the same input, so keep only the first one.
    deduplicated = eligible.drop_duplicates(
        subset="_program_text",
        keep="first",
    )

    diagnostics = {
        "analysis_client_id": analysis_client_id,
        "positive_program_expense_id": positive_id,
        "positive_similarity": positive_similarity,
        "positive_rank": to_int(positive["distance_rank"]),
        "non_positive_candidates": total_non_positive,
        "excluded_easy_negative": int(is_easy_negative.sum()),
        "excluded_positive_text_duplicate": int(
            (is_positive_duplicate & ~is_easy_negative).sum()
        ),
        "excluded_margin_le_min": int(
            (
                ~above_min_margin
                & ~is_easy_negative
                & ~is_positive_duplicate
            ).sum()
        ),
        "excluded_duplicate_program_text": (
            len(eligible) - len(deduplicated)
        ),
        "eligible_semi_hard": len(deduplicated),
    }

    selected = deduplicated.drop(columns="_program_text")

    selected["positive_program_expense_id"] = positive_id
    selected["positive_similarity"] = positive_similarity
    selected["positive_rank"] = diagnostics["positive_rank"]
    selected["positive_program_category"] = clean_value(
        positive["program_category"]
    )
    selected["positive_program_subcategory"] = clean_value(
        positive["program_subcategory"]
    )
    selected["positive_program_expense_description"] = clean_value(
        positive["program_expense_description"]
    )

    return (
        selected,
        diagnostics,
    )


def to_validator_row(
    row: pd.Series,
) -> pd.Series:

    """
    Same field names that static_validate_pair() and the validator
    prompt use. Client and program share the same expense_class.
    """

    expense_class = clean_value(row["expense_class"])

    return pd.Series(
        {
            "client_expense_title": clean_value(row["client_expense_title"]),
            "client_expense_description": clean_value(
                row["client_expense_description"]
            ),
            "client_expense_class": expense_class,
            "program_category": clean_value(row["program_category"]),
            "program_subcategory": clean_value(row["program_subcategory"]),
            "program_expense_description": clean_value(
                row["program_expense_description"]
            ),
            "program_expense_class": expense_class,
        }
    )


# ============================================================
# Sequential validation
# ============================================================

def run_pilot(
    pairs_df: pd.DataFrame,
    sampled_df: pd.DataFrame,
    easy_negative_keys: set[tuple[int, int | str]],
) -> tuple[pd.DataFrame, pd.DataFrame, int]:

    cache = VerdictCache()

    prompt_hash = compute_prompt_hash()

    llm = None

    llm_calls = 0

    log_rows = []

    client_rows = []

    for client_counter, sampled in enumerate(
        sampled_df.itertuples(index=False),
        start=1,
    ):

        analysis_client_id = int(sampled.analysis_client_id)

        client_df = pairs_df[
            pairs_df["analysis_client_id"] == analysis_client_id
        ]

        (
            candidates,
            diagnostics,
        ) = select_semi_hard_candidates(
            client_df,
            easy_negative_keys,
        )

        print()
        print("=" * 70)
        print(f"Client {client_counter}/{len(sampled_df)}")
        print("analysis_client_id:", analysis_client_id)
        print("expense_class:", sampled.expense_class)
        print("eligible semi-hard candidates:", len(candidates))

        position = 0

        first_invalid_position = None

        for _, row in candidates.iterrows():

            if position >= MAX_CANDIDATES_PER_CLIENT:
                break

            validator_row = to_validator_row(row)

            log_row = {
                "analysis_client_id": analysis_client_id,
                "expense_class": sampled.expense_class,
                "pilot_group": sampled.pilot_group,
                "program_expense_id": to_int(
                    row["candidate_program_expense_id"]
                ),
                "positive_program_expense_id": to_int(
                    row["positive_program_expense_id"]
                ),
                "positive_similarity": float(row["positive_similarity"]),
                "candidate_similarity": float(row["cosine_similarity"]),
                "margin": float(row["margin"]),
                "candidate_distance_rank": to_int(row["distance_rank"]),
                "positive_rank": to_int(row["positive_rank"]),
                "rank_offset": (
                    to_int(row["distance_rank"])
                    - to_int(row["positive_rank"])
                ),
                "candidate_position": None,
                "non_fundable": False,
                "non_fundable_source_fields": "",
                "static_status": "",
                "cache_hit": False,
                "used_llm": False,
                "verdict": "",
                "reason": "",
                "verdict_source": "",
                "pair_fingerprint": "",
                "label": None,
                "mining_strategy": MINING_STRATEGY,
                **validator_row.to_dict(),
                "positive_program_category": row["positive_program_category"],
                "positive_program_subcategory": row[
                    "positive_program_subcategory"
                ],
                "positive_program_expense_description": row[
                    "positive_program_expense_description"
                ],
            }

            # ------------------------------------------------
            # Non-fundable: skip, no LLM, no label,
            # does not use up a candidate slot.
            # ------------------------------------------------

            (
                non_fundable,
                non_fundable_fields,
            ) = detect_non_fundable_pair(row)

            if non_fundable:

                log_row.update(
                    {
                        "non_fundable": True,
                        "non_fundable_source_fields": ", ".join(
                            non_fundable_fields
                        ),
                        "verdict": "SKIPPED_NON_FUNDABLE",
                        "reason": (
                            "Exact non-fundable marker found; "
                            "no negative label assigned."
                        ),
                        "verdict_source": "non_fundable_skip",
                    }
                )

                log_rows.append(log_row)

                print("Non-fundable -> SKIP")

                continue

            position += 1

            log_row["candidate_position"] = position

            # ------------------------------------------------
            # Static validation (free)
            # ------------------------------------------------

            (
                static_status,
                static_reason,
            ) = static_validate_pair(validator_row)

            log_row["static_status"] = static_status

            if static_status == "UNCERTAIN":

                verdict = "UNCERTAIN"

                log_row.update(
                    {
                        "verdict": verdict,
                        "reason": static_reason,
                        "verdict_source": "static_validation",
                    }
                )

            else:

                pair_fingerprint = compute_pair_fingerprint(validator_row)

                log_row["pair_fingerprint"] = pair_fingerprint

                cached = cache.get(
                    pair_fingerprint,
                    prompt_hash,
                )

                if cached is not None:

                    verdict = cached["verdict"]

                    log_row.update(
                        {
                            "cache_hit": True,
                            "verdict": verdict,
                            "reason": cached["reason"],
                            "verdict_source": cached["source"],
                        }
                    )

                else:

                    if llm is None:
                        llm = create_llm(temperature=0.0)

                    result = validate_pair_with_retry(
                        llm=llm,
                        row=validator_row,
                    )

                    llm_calls += 1

                    # Persist immediately: a crash must not lose
                    # an LLM call that was already paid for.
                    cache.add(
                        analysis_client_id=analysis_client_id,
                        candidate_program_expense_id=log_row[
                            "program_expense_id"
                        ],
                        pair_fingerprint=pair_fingerprint,
                        prompt_hash=prompt_hash,
                        result=result,
                        source=SOURCE,
                    )

                    verdict = result.verdict

                    log_row.update(
                        {
                            "used_llm": True,
                            "verdict": verdict,
                            "reason": result.reason,
                            "verdict_source": SOURCE,
                        }
                    )

            print(
                f"Position {position} | rank "
                f"{log_row['candidate_distance_rank']} | margin "
                f"{log_row['margin']:.5f} | "
                f"{'cache' if log_row['cache_hit'] else 'llm' if log_row['used_llm'] else 'static'}"
                f" -> {verdict}"
            )

            # ------------------------------------------------
            # INVALID -> confirmed negative, STOP this client.
            # VALID / UNCERTAIN -> next candidate.
            # ------------------------------------------------

            if verdict == "INVALID":

                log_row["label"] = 0

                log_rows.append(log_row)

                first_invalid_position = position

                print("Confirmed semi-hard negative -> STOP client.")

                break

            log_rows.append(log_row)

        if first_invalid_position is None:
            print("No confirmed semi-hard negative for this client.")

        client_rows.append(
            {
                **diagnostics,
                "expense_class": sampled.expense_class,
                "pilot_group": sampled.pilot_group,
                "candidate_slots": min(
                    MAX_CANDIDATES_PER_CLIENT,
                    diagnostics["eligible_semi_hard"],
                ),
                "evaluated_candidates": position,
                "hard_negative_found": first_invalid_position is not None,
                "first_invalid_position": first_invalid_position,
            }
        )

    return (
        pd.DataFrame(log_rows),
        pd.DataFrame(client_rows),
        llm_calls,
    )


# ============================================================
# Metrics
# ============================================================

def summarize(
    log_df: pd.DataFrame,
    clients_df: pd.DataFrame,
) -> dict:

    evaluated = log_df[~log_df["non_fundable"]]

    negatives = int((evaluated["verdict"] == "INVALID").sum())

    # Cost is attributed by verdict source, so it stays correct when the
    # pilot is re-run and its own earlier verdicts come from the cache.
    pilot_llm_calls = int((evaluated["verdict_source"] == SOURCE).sum())

    reused_from_easy = int((evaluated["verdict_source"] == "easy").sum())

    candidate_slots = int(clients_df["candidate_slots"].sum())

    first_positions = clients_df["first_invalid_position"].dropna()

    summary = {
        "selected_clients": len(clients_df),
        "clients_without_candidates": int(
            (clients_df["eligible_semi_hard"] == 0).sum()
        ),
        "theoretical_max_llm_calls": candidate_slots,
        "candidate_pairs_considered": len(evaluated),
        "actual_llm_calls": pilot_llm_calls,
        "llm_calls_this_run": int(evaluated["used_llm"].sum()),
        "cache_hits_this_run": int(evaluated["cache_hit"].sum()),
        "calls_avoided_by_cache": reused_from_easy,
        "static_uncertain": int(
            (evaluated["static_status"] == "UNCERTAIN").sum()
        ),
        "calls_avoided_by_early_stop": candidate_slots - len(evaluated),
        "non_fundable_skips": int(log_df["non_fundable"].sum()),
    }

    for verdict in ["VALID", "INVALID", "UNCERTAIN"]:

        count = int((evaluated["verdict"] == verdict).sum())

        summary[f"{verdict.lower()}_count"] = count

        summary[f"{verdict.lower()}_rate"] = (
            count / len(evaluated) if len(evaluated) else None
        )

    summary.update(
        {
            "clients_with_hard_negative": negatives,
            "hit_rate": (
                negatives / len(clients_df) if len(clients_df) else None
            ),
            "llm_calls_per_hard_negative": (
                pilot_llm_calls / negatives if negatives else None
            ),
            "evaluations_per_hard_negative": (
                len(evaluated) / negatives if negatives else None
            ),
            "first_invalid_at_1": int((first_positions == 1).sum()),
            "first_invalid_at_2": int((first_positions == 2).sum()),
            "first_invalid_at_3": int((first_positions == 3).sum()),
        }
    )

    margins = evaluated["margin"]

    ranks = evaluated["candidate_distance_rank"]

    for name, values in [("margin", margins), ("candidate_rank", ranks)]:

        summary.update(
            {
                f"{name}_min": values.min() if len(values) else None,
                f"{name}_median": values.median() if len(values) else None,
                f"{name}_max": values.max() if len(values) else None,
            }
        )

    summary.update(
        {
            "positive_rank_median": clients_df["positive_rank"].median(),
            "positive_rank_max": clients_df["positive_rank"].max(),
            "rank_offset_median": (
                evaluated["rank_offset"].median() if len(evaluated) else None
            ),
        }
    )

    return summary


def build_metric_tables(
    log_df: pd.DataFrame,
    clients_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:

    overall = pd.DataFrame(
        [
            {
                "metric": metric,
                "value": value,
            }
            for metric, value in summarize(log_df, clients_df).items()
        ]
    )

    class_rows = []

    for expense_class, class_clients in clients_df.groupby(
        "expense_class",
        sort=True,
    ):

        class_rows.append(
            {
                "expense_class": expense_class,
                "pilot_group": class_clients["pilot_group"].iloc[0],
                **summarize(
                    log_df[log_df["expense_class"] == expense_class],
                    class_clients,
                ),
            }
        )

    by_class = pd.DataFrame(class_rows)

    evaluated = log_df[~log_df["non_fundable"]].copy()

    evaluated["is_invalid"] = evaluated["verdict"] == "INVALID"

    evaluated["margin_bucket"] = pd.cut(
        evaluated["margin"],
        bins=MARGIN_BUCKET_EDGES,
        right=True,
    ).astype(str)

    by_margin = (
        evaluated
        .groupby("margin_bucket", sort=False)
        .agg(
            pairs=("verdict", "size"),
            invalid=("is_invalid", "sum"),
            invalid_rate=("is_invalid", "mean"),
            valid=("verdict", lambda v: (v == "VALID").sum()),
            uncertain=("verdict", lambda v: (v == "UNCERTAIN").sum()),
            margin_min=("margin", "min"),
            margin_max=("margin", "max"),
        )
        .reset_index()
        .sort_values("margin_min")
    )

    by_position = (
        evaluated
        .groupby("candidate_position")
        .agg(
            pairs=("verdict", "size"),
            invalid=("is_invalid", "sum"),
            invalid_rate=("is_invalid", "mean"),
            margin_median=("margin", "median"),
            rank_offset_median=("rank_offset", "median"),
        )
        .reset_index()
    )

    return (
        overall,
        by_class,
        by_margin,
        by_position,
    )


# ============================================================
# Main
# ============================================================

def main() -> None:

    assert_non_fundable_detection()

    pairs_df = pd.read_csv(
        DISTANCE_ANALYSIS_PATH,
        encoding="utf-8-sig",
    )

    pairs_df["is_known_positive"] = pairs_df["is_known_positive"].apply(
        to_bool
    )

    # --------------------------------------------------------
    # Confirm the actual class names before sampling
    # --------------------------------------------------------

    actual_classes = sorted(pairs_df["expense_class"].unique().tolist())

    group_by_class = resolve_class_groups(actual_classes)

    print("=" * 70)
    print("EXPENSE CLASSES (actual values)")
    print("=" * 70)

    for expense_class in actual_classes:
        print(f"[{group_by_class[expense_class]:8}] {expense_class}")

    sampled_df = sample_pilot_clients(
        pairs_df,
        group_by_class,
    )

    easy_negative_keys = load_easy_negative_keys()

    # --------------------------------------------------------
    # Pilot validation
    # --------------------------------------------------------

    (
        log_df,
        clients_df,
        llm_calls,
    ) = run_pilot(
        pairs_df,
        sampled_df,
        easy_negative_keys,
    )

    # --------------------------------------------------------
    # Confirmed semi-hard negatives
    # --------------------------------------------------------

    negatives_df = log_df[log_df["verdict"] == "INVALID"].copy()

    negatives_df["label"] = 0

    final_columns = [
        "program_expense_id",
        "client_expense_title",
        "client_expense_description",
        "client_expense_class",
        "program_category",
        "program_subcategory",
        "program_expense_description",
        "program_expense_class",
        "label",
        # Mining metadata (not part of the label)
        "analysis_client_id",
        "positive_program_expense_id",
        "margin",
        "candidate_position",
        "mining_strategy",
    ]

    hard_negatives_df = negatives_df[final_columns].reset_index(drop=True)

    # --------------------------------------------------------
    # Safety assertions
    # --------------------------------------------------------

    assert (hard_negatives_df["label"] == 0).all()

    assert not hard_negatives_df.duplicated(
        ["analysis_client_id", "program_expense_id"]
    ).any()

    assert (
        hard_negatives_df["program_expense_id"]
        != hard_negatives_df["positive_program_expense_id"]
    ).all()

    assert (
        hard_negatives_df["client_expense_class"].apply(normalize_class)
        == hard_negatives_df["program_expense_class"].apply(normalize_class)
    ).all()

    assert not any(
        (
            to_int(row["analysis_client_id"]),
            to_int(row["program_expense_id"]),
        )
        in easy_negative_keys
        or (
            to_int(row["analysis_client_id"]),
            program_text_key(
                row["program_category"],
                row["program_subcategory"],
                row["program_expense_description"],
            ),
        )
        in easy_negative_keys
        for _, row in hard_negatives_df.iterrows()
    ), "A semi-hard negative is already an easy negative (by id or text)."

    assert not any(
        detect_non_fundable_pair(row)[0]
        for _, row in hard_negatives_df.iterrows()
    ), "Semi-hard negatives contain non-fundable values."

    assert (
        clients_df["evaluated_candidates"] <= MAX_CANDIDATES_PER_CLIENT
    ).all()

    assert llm_calls == int(log_df["used_llm"].sum())

    # --------------------------------------------------------
    # Metrics and outputs
    # --------------------------------------------------------

    (
        overall_df,
        by_class_df,
        by_margin_df,
        by_position_df,
    ) = build_metric_tables(
        log_df,
        clients_df,
    )

    review_columns = [
        "manual_review_verdict",
        "manual_review_notes",
    ]

    review_df = log_df[
        log_df["verdict"].isin(["INVALID", "VALID"])
    ].copy()

    for column in review_columns:
        review_df[column] = ""

    review_df = review_df[
        review_columns
        + [
            "verdict",
            "reason",
            "analysis_client_id",
            "expense_class",
            "program_expense_id",
            "margin",
            "candidate_position",
            "client_expense_title",
            "client_expense_description",
            "program_category",
            "program_subcategory",
            "program_expense_description",
            "positive_program_category",
            "positive_program_subcategory",
            "positive_program_expense_description",
        ]
    ].sort_values(["verdict", "expense_class", "analysis_client_id"])

    hard_negatives_df.to_csv(
        HARD_NEGATIVES_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    hard_negatives_df.to_excel(
        HARD_NEGATIVES_EXCEL_PATH,
        index=False,
        engine="openpyxl",
    )

    settings_df = pd.DataFrame(
        [
            {"setting": "pilot_seed", "value": PILOT_SEED},
            {
                "setting": "max_candidates_per_client",
                "value": MAX_CANDIDATES_PER_CLIENT,
            },
            {"setting": "min_margin", "value": MIN_MARGIN},
            {
                "setting": "clients_per_main_class",
                "value": CLIENTS_PER_GROUP["main"],
            },
            {
                "setting": "clients_per_control_class",
                "value": CLIENTS_PER_GROUP["control"],
            },
            {"setting": "prompt_hash", "value": compute_prompt_hash()},
        ]
    )

    with pd.ExcelWriter(
        PILOT_REPORT_PATH,
        engine="openpyxl",
    ) as writer:

        overall_df.to_excel(writer, sheet_name="summary", index=False)
        by_class_df.to_excel(writer, sheet_name="by_class", index=False)
        by_margin_df.to_excel(writer, sheet_name="by_margin_bucket", index=False)
        by_position_df.to_excel(writer, sheet_name="by_position", index=False)
        log_df.to_excel(writer, sheet_name="validation_log", index=False)
        clients_df.to_excel(writer, sheet_name="clients", index=False)
        review_df.to_excel(writer, sheet_name="manual_review", index=False)
        hard_negatives_df.to_excel(
            writer,
            sheet_name="hard_negatives",
            index=False,
        )
        settings_df.to_excel(writer, sheet_name="settings", index=False)

    # --------------------------------------------------------
    # Console summary
    # --------------------------------------------------------

    summary = dict(zip(overall_df["metric"], overall_df["value"]))

    print()
    print("=" * 70)
    print("SEMI-HARD NEGATIVE PILOT COMPLETE")
    print("=" * 70)

    for metric in [
        "selected_clients",
        "theoretical_max_llm_calls",
        "candidate_pairs_considered",
        "actual_llm_calls",
        "llm_calls_this_run",
        "calls_avoided_by_cache",
        "calls_avoided_by_early_stop",
        "non_fundable_skips",
        "valid_count",
        "invalid_count",
        "uncertain_count",
        "clients_with_hard_negative",
        "hit_rate",
        "llm_calls_per_hard_negative",
    ]:
        print(f"{metric}: {summary[metric]}")

    print()
    print("Pilot report:", PILOT_REPORT_PATH)
    print("Semi-hard negatives CSV:", HARD_NEGATIVES_CSV_PATH)
    print("Semi-hard negatives Excel:", HARD_NEGATIVES_EXCEL_PATH)
    print("=" * 70)


if __name__ == "__main__":
    main()
