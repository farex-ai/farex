# Stage 3 new tasks: owner reading copy (reviewed by the owner 2026-11-03)

Generated from `stage3/tasks_new_draft.json`. These tasks have never been shown to a model.
Validation: `python3 -m farex_pilot.stage3 --validate` must report every check passed across all 40 tasks. Mark anything ambiguous, too easy or wrong.

## Math

### math_h1  (M1, M3)

> A shop that opened in 2015 sells notebooks at $3.50 each and pens at $1.25 each. Maya buys 4 notebooks and 2 pens and pays with a $20 bill. How much change does she get, in dollars?

- **Correct:** Final number given is 3.5 (dollars).
- **Working:** `20 - (4 * 3.50 + 2 * 1.25)` = 3.5

### math_h2  (M2, M3)

> A jacket costs $80 and the store has 12 of them in stock. The jacket is discounted by 25%, and then a 10% sales tax is added to the discounted price. What is the final price of one jacket, in dollars?

- **Correct:** Final number given is 66 (dollars).
- **Working:** `80 * (1 - 25 / 100) * (1 + 10 / 100)` = 66

### math_h3  (M1, M4)

> Eggs are sold only in full cartons of 12. A bakery needs 100 eggs for Monday and 45 eggs for Tuesday and buys all of them at once. How many cartons must it buy?

- **Correct:** Final number given is 13 (cartons).
- **Working:** `ceil((100 + 45) / 12)` = 13

### math_h4  (M1, M3)

> A 4-year-old car travels 150 kilometres in 2.5 hours, then 90 kilometres in 1.5 hours. What is its average speed for the whole trip, in kilometres per hour?

- **Correct:** Final number given is 60 (km/h).
- **Working:** `(150 + 90) / (2.5 + 1.5)` = 60

### math_h5  (M1, M2)

> A recipe for 4 people uses 300 grams of flour. How many kilograms of flour are needed for 10 people?

- **Correct:** Final number given is 0.75 (kilograms).
- **Working:** `300 / 4 * 10 / 1000` = 0.75

### math_h6  (M1, M2)

> A water tank holds 2,000 litres and is 35% full. Water is added at 40 litres per minute. How many minutes until the tank is full?

- **Correct:** Final number given is 32.5 (minutes).
- **Working:** `(2000 - 2000 * 35 / 100) / 40` = 32.5

## Extraction

### extraction_h1  (E1, E2, E3, E4)

> From this text: 'Our old office was at 14 Elm Street. In March we moved to 220 Harbor Road, and our warehouse remains at 9 Mill Lane.', extract the current office address.

- **Correct:** Gives 220 Harbor Road and neither the old office nor the warehouse address.
- **Must not include:** 14 Elm Street, 9 Mill Lane

### extraction_h2  (E1, E2, E3, E4)

> From this text: 'For billing questions write to billing@northwind.example. Support used to be at help@northwind.example, but since June it is support@northwind.example. Press enquiries go to press@northwind.example.', extract the current support email address.

- **Correct:** Gives support@northwind.example and none of the other three addresses.
- **Must not include:** billing@northwind.example, help@northwind.example, press@northwind.example

### extraction_h3  (E1, E2, E3, E4)

> From this text: 'The conference was first planned for April 3. It was moved to April 17 because of the venue, and the follow-up workshop is on May 2.', extract the date the conference will take place.

- **Correct:** Gives April 17 (any common spelling) and neither April 3 nor May 2.
- **Accepted spellings:** April 17, April 17th, 17 April, 17th April, Apr 17, 17th of April
- **Must not include:** April 3, April 3rd, 3 April, 3rd April, 3rd of April, Apr 3, May 2, May 2nd, 2 May, 2nd May, 2nd of May
- **Changed after the owner's read:** the spellings above were added 2026-11-04 by the audit. The task text did not change.

### extraction_h4  (E1, E2, E3, E4)

> From this text: 'The report was drafted by Lena Ortiz and reviewed by Tom Becker. Final approval came from Priya Nair, the department head.', extract the name of the person who approved the report.

- **Correct:** Gives Priya Nair and neither of the other two names.
- **Must not include:** Lena Ortiz, Tom Becker

### extraction_h5  (E1, E3, E4)

> From this text: 'The invoice lists a subtotal of $840.00, a shipping fee of $35.00 and a discount of $50.00. The amount due is $825.00.', extract the amount due.

- **Correct:** Gives the amount 825 (e.g. $825.00) and none of the other amounts in the passage.
- **Must not include:** 840, 35, 50

### extraction_h6  (E1, E2, E3, E4)

> From this text: 'Call the front desk at 555-0142. After 6 pm, calls are forwarded to the night line, 555-0187. The fax number is 555-0199.', extract the phone number to call after 6 pm.

- **Correct:** Gives 555-0187 and neither the front desk nor the fax number.
- **Must not include:** 555-0142, 555-0199

## Evidence QA

### evidence_qa_h1  (Q1, Q4)

> Claim: 'The Linden Library lends e-readers to all members'. Text: 'The Linden Library lends e-readers to all members except those under 16, who can borrow audiobooks instead.' Answer yes/no and cite one phrase.

- **Correct:** Verdict no, with a quoted phrase showing members under 16 are excluded.
- **Accepted quote(s):** “under 16” · needs: an exception means not all members

### evidence_qa_h2  (Q2, Q4)

> Claim: 'The Arden Bridge is longer than 1 kilometre'. Text: 'The Arden Bridge spans 1,240 metres across the river. It was repainted in 2019.' Answer yes/no and cite one phrase.

- **Correct:** Verdict yes, with a quoted phrase showing the bridge spans 1,240 metres.
- **Accepted quote(s):** “1,240 metres” · needs: 1 kilometre is 1,000 metres

### evidence_qa_h3  (Q2, Q3)

> Claim: 'The 8:15 train reaches Halden before 9:00'. Text: 'The 8:15 train from Carrow stops at Bell Hill after 25 minutes. From Bell Hill it takes another 30 minutes to reach Halden.' Answer yes/no and cite one phrase.

- **Correct:** Verdict no, with a quoted phrase giving one of the two travel times (8:15 + 25 + 30 minutes = 9:10).
- **Accepted quote(s):** “25 minutes”; “30 minutes” · needs: adding minutes to a clock time

### evidence_qa_h4  (Q1, Q4)

> Claim: 'Pets are allowed in the Juniper Hotel's garden rooms'. Text: 'Pets are not allowed in the Juniper Hotel's main building. The garden rooms are the only rooms where pets may stay.' Answer yes/no and cite one phrase.

- **Correct:** Verdict yes, with a quoted phrase showing pets may stay in the garden rooms.
- **Accepted quote(s):** “pets may stay” · needs: none

### evidence_qa_h5  (Q2, Q4)

> Claim: 'The Kell Reservoir was more than half full in August'. Text: 'The Kell Reservoir can hold 8 million litres. In August it held 3.6 million litres, down from 5.1 million in June.' Answer yes/no and cite one phrase.

- **Correct:** Verdict no, with a quoted phrase showing it held 3.6 million litres in August.
- **Accepted quote(s):** “3.6 million litres” · needs: half of 8 million is 4 million

### evidence_qa_h6  (Q2, Q3)

> Claim: 'Dana finished the race ahead of Omar'. Text: 'Omar finished the race in third place. Dana finished four places ahead of Omar's teammate Lee, who came fifth.' Answer yes/no and cite one phrase.

- **Correct:** Verdict yes, with a quoted phrase showing Dana finished four places ahead of Lee (fifth), i.e. first.
- **Accepted quote(s):** “four places ahead” · needs: counting places

## Coding

### coding_h1  (C1, C2)

> Write a Python function named run_length_encode(s) that compresses a string by replacing each run of the same character with the character followed by the length of the run, for example 'aaabcc' becomes 'a3b1c2'. An empty string returns an empty string. The function is case-sensitive, so 'aA' becomes 'a1A1'.

- **Correct:** Defines run_length_encode(s) that passes the hidden tests.
- **Hidden tests:** `run_length_encode("")` → `""`; `run_length_encode("a")` → `"a1"`; `run_length_encode("aaabcc")` → `"a3b1c2"`; `run_length_encode("aA")` → `"a1A1"`; `run_length_encode("zzzzzzzzzzzz")` → `"z12"`; `run_length_encode("abca")` → `"a1b1c1a1"`

### coding_h2  (C1, C2)

> Write a Python function named running_max(nums) that returns a new list where each element is the largest value seen so far in nums. An empty list returns an empty list. The list may contain negative numbers.

- **Correct:** Defines running_max(nums) that passes the hidden tests.
- **Hidden tests:** `running_max([])` → `[]`; `running_max([3])` → `[3]`; `running_max([1, 3, 2, 5, 4])` → `[1, 3, 3, 5, 5]`; `running_max([-5, -2, -7])` → `[-5, -2, -2]`; `running_max([2, 2, 1])` → `[2, 2, 2]`

### coding_h3  (C1, C3)

> Write a Python function named count_words(text) that returns a dictionary mapping each word to how many times it appears. Words are separated by one or more spaces, and counting ignores case, so keys are lowercase ('The' and 'the' are the same word). An empty string returns an empty dictionary.

- **Correct:** Defines count_words(text) that passes the hidden tests.
- **Hidden tests:** `count_words("")` → `{}`; `count_words("a")` → `{"a": 1}`; `count_words("The the THE")` → `{"the": 3}`; `count_words("red blue red")` → `{"red": 2, "blue": 1}`; `count_words("  spaced   out ")` → `{"spaced": 1, "out": 1}`

### coding_h4  (C1, C2)

> Write a Python function named second_largest(nums) that returns the second largest distinct value in the list nums. If there are fewer than two distinct values (including an empty list), return None. The list may contain negative numbers and repeated values.

- **Correct:** Defines second_largest(nums) that passes the hidden tests.
- **Hidden tests:** `second_largest([3, 1, 2])` → `2`; `second_largest([5, 5, 4])` → `4`; `second_largest([7])` → `null`; `second_largest([])` → `null`; `second_largest([-1, -3, -2])` → `-2`; `second_largest([2, 2])` → `null`

### coding_h5  (C1, C2)

> Write a Python function named is_balanced(s) that returns True if the brackets (), [] and {} in the string s are balanced and correctly nested, and False otherwise. Characters other than brackets are ignored. An empty string is balanced.

- **Correct:** Defines is_balanced(s) that passes the hidden tests.
- **Hidden tests:** `is_balanced("")` → `true`; `is_balanced("()")` → `true`; `is_balanced("([]{})")` → `true`; `is_balanced("(]")` → `false`; `is_balanced("(()")` → `false`; `is_balanced("a(b)c")` → `true`; `is_balanced(")(")` → `false`; `is_balanced("([)]")` → `false`

### coding_h6  (C1, C2)

> Write a Python function named chunk_list(lst, n) that splits the list lst into consecutive chunks of size n and returns a list of those chunks; the last chunk may be shorter. An empty list returns an empty list, and if n is 0 or negative, return an empty list.

- **Correct:** Defines chunk_list(lst, n) that passes the hidden tests.
- **Hidden tests:** `chunk_list([1, 2, 3, 4, 5], 2)` → `[[1, 2], [3, 4], [5]]`; `chunk_list([], 3)` → `[]`; `chunk_list([1, 2], 5)` → `[[1, 2]]`; `chunk_list([1, 2, 3], 1)` → `[[1], [2], [3]]`; `chunk_list([1, 2, 3], 0)` → `[]`; `chunk_list([1, 2, 3], -1)` → `[]`
