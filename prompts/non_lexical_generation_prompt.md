Generate new synthetic client expense records for the program expense
below.

==================================================
PROGRAM EXPENSE ANCHOR
==================================================

Expense class:
{expense_class}

Category:
{category}

Subcategory:
{subcategory}


==================================================
EXAMPLES
==================================================

Real, human-validated client expenses that match this program expense.
Each one is a valid match although it shares no words with the
Category and Subcategory. Each example has a row number:

{few_shot_examples}


==================================================
TASK
==================================================

For EACH example above, generate {n_per_example} new client expenses
that follow the same pattern as that example:

- a concrete, specific expense, not a general category,
- no words from the Category or Subcategory,
  also not in another grammatical form,
- clearly covered by the program expense,
- different from each other and from all other generated expenses,
- do NOT copy or paraphrase the example; write new expenses.

For every generated expense, set "source_row" to the row number of
the example it was generated from.

Every generated expense must have exactly this expense_class:

{expense_class}

Return exactly this JSON structure:

{
  "expenses": [
    {
      "source_row": 0,
      "expense_title": "...",
      "expense_description": "...",
      "expense_class": "..."
    }
  ]
}
