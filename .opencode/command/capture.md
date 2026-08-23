---
description: Capture an emergent idea as a queue-labeled issue, then immediately resume prior work
---

Capture the following idea without derailing current work: $ARGUMENTS

1. If $ARGUMENTS is empty, ask the user for a one-line description first.
2. Check for duplicates: `gh issue list --state open --limit 50`. If an open issue already covers the same work, report and link it instead of creating a second issue, then resume prior work.
3. Create the issue: `gh issue create --label queue --title "[queue] <concise title>" --body-file "<temp file>"`, using this body template:

   ```
   **What**
   - <what this work is, one plain sentence per bullet, max 3 bullets>

   **Done when**
   - each bullet states an observable behavior or a directly checkable fact — prefer the shape "when <something happens>, <the system does <something>>"
   - someone reading only this section must be able to judge pass or fail without asking questions

   **Out of scope**
   - <things this work must NOT try to do; write "nothing special" if none>

   **Why it is waiting**
   - <the reason nothing is happening yet; omit if not applicable>

   **What would start it**
   - <the event or decision that moves this forward; "a person decides" is a valid answer>

   **Where this came from**
   - <file path, link, or "chat with owner on YYYY-MM-DD">
   ```

   Write plain English. Short bullets. No jargon or abbreviations. Every "Done when" bullet must be something you can directly check. If "What" needs more than 3 bullets, split this into several smaller issues.
4. If step 3 fails, STOP and show the error — do NOT resume prior work until the issue exists.
5. Print the returned issue URL.
6. Immediately return to whatever was being worked on before the capture.

Rule: capture over execute — never start working on the captured item unless the user explicitly says so.
