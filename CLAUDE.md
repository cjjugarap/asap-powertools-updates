# ASAP Powertools — Project Memory

## Screen Mapping Method

Screens are mapped using the ASAP Page Inspector Chrome extension. The inspector captures interactive elements (role, label, ID) and groups them by section. Each paste becomes a node in `screen_map.json`. Screens are nodes, user actions are edges.

### Lessons Learned: Screen Mapping Gaps

The inspector captures **structure** (what elements exist) but not **behavior** (what happens after you interact with them). Three failure modes to watch for:

1. **Interactions that reveal new elements.**
   Selecting a value from a dropdown or clicking a tab can trigger an ASP.NET postback that renders new fields or buttons. Run the inspector *again after each significant interaction* to capture newly-revealed elements before moving to the next step.
   > Example: Selecting "High School Diploma" from the Credits tab revealed Diploma Date, Graduation Date fields, and a Save button (`btnsavecreditprogramdetails`) that weren't visible on the initial inspector run.

2. **Screens that duplicate a selector from a previous screen.**
   A value set on one screen may need to be set again on a subsequent screen. Always ask: "Does the next screen have its own version of something we already configured?"
   > Example: The transcript popup (`CustomerTranscript.aspx`) has its own Kendo program selector (`cmbProgram`) that also requires "High School Diploma" — separate from the one on the Credits tab.

3. **Button outcomes that differ from their label.**
   "Print" does not always open a browser print dialog — it may trigger a file download. Verify what each button *produces* rather than assuming from its name.
   > Example: `btnPrint` in the transcript popup downloads a PDF, not a browser dialog.

### Date swap logic

`swap_dates` checks whether **graduation date < diploma date** (i.e., the dates are in the wrong order) and swaps them if so. The correct order is diploma date ≤ graduation date. Do NOT swap when diploma < graduation — that is already correct.

### Mapping Protocol (going forward)

For each step in a process being mapped:
- Run the inspector **before** the action to capture the current state.
- Perform the action.
- Run the inspector **again** to capture any newly-revealed elements.
- Explicitly ask: "What did this action produce?" (new elements, navigation, download, dialog?)

---

## PII Protection — Non-Negotiable Design Constraint

No personally identifiable information is ever captured, stored, transmitted, or logged anywhere in this pipeline. This applies to every component and every future feature.

### Inspector (mapping side)
- Field **values** are always replaced with `[REDACTED]` — never stored
- Data rows (student names, emails, phones, DOBs, addresses, IDs) are suppressed entirely from output
- Only labels, element IDs, and page structure are captured
- Regex backstop catches anything that slips through (email patterns, phone shapes, SSN, DOB formats)

### Navigator (recording side)
- `isSecretField()` detects password/OTP/credit card fields — values are withheld from the recording JSON and flagged for the encrypted credentials store instead
- The recorder captures field values only on `change`, and only for non-sensitive fields
- The encrypted credentials store (PBKDF2 + Fernet AES) never leaves the local machine

### LLM cleanup step (planned)
- Before any recording is sent to Claude for path cleaning, all `fill` step values must be stripped to `[REDACTED]`
- Only step types and element identifiers (role, label, ID) travel to the API — never field contents

### Rule
> **The full pipeline — inspect → record → clean → replay — must be PII-free end to end.**
> If a future feature would require capturing a real value to function, it must use the local encrypted credentials store, not plaintext storage or any external service.

---

## Key Files

| File | Purpose |
|------|---------|
| `screen_map.json` | Screen graph — nodes (screens) + edges (transitions) + named processes |
| `templates/print_hsd_transcript.json` | Batch template for printing HSD transcripts |
| `inspector-extension/` | Chrome extension that injects the page inspector |
| `page_inspector_bookmarklet.html` | Bookmarklet installer (superseded by extension due to CSP) |
| `navigator.py` | Playwright-based browser automation recorder and batch runner |
| `navigator_core.py` | Pure helper functions shared by navigator.py |

## Branch

Active development: `claude/jolly-pascal-4pz9rt`
