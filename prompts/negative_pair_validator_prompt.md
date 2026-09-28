Evaluate the following client-program expense pair.

==================================================
CLIENT EXPENSE
==================================================

Title:
{client_expense_title}

Description:
{client_expense_description}

Expense class:
{client_expense_class}


==================================================
PROGRAM EXPENSE
==================================================

Category:
{program_category}

Subcategory:
{program_subcategory}

Expense description:
{program_expense_description}

Expense class:
{program_expense_class}


==================================================
TASK
==================================================

Independently determine whether the CLIENT EXPENSE is a valid
semantic/business match for this specific PROGRAM EXPENSE.

Do NOT assume that the pair is negative simply because it was selected
as a candidate negative.

Use the following priority:

1. Program Category
2. Program Subcategory
3. Program Expense Description as secondary supporting information

Interpret the result as follows:

VALID:
The client expense clearly fits the Program Category and the available
Program Subcategory information, and the Program Expense Description
does not contradict or exclude the match.

INVALID:
There is sufficient information to determine that the client expense
does not fit the Program Category or Program Subcategory, or the Program
Expense Description explicitly excludes, contradicts or restricts the
client expense.

UNCERTAIN:
The available Category/Subcategory information is missing, incomplete,
too broad or otherwise insufficient to reliably determine whether the
client expense matches the Program Expense.

Important:

- Different wording does not imply INVALID.
- Similar wording does not imply VALID.
- Focus on semantic meaning and business compatibility.
- Do not invent missing business rules.
- If the Program Category is broad, use the Program Subcategory to make
  the more specific decision.
- Use the Program Expense Description mainly for additional clarification,
  explicit inclusions, exclusions, restrictions and conditions.

Return exactly:

{
  "verdict": "VALID | INVALID | UNCERTAIN",
  "reason": "short and specific explanation based mainly on Category and Subcategory"
}