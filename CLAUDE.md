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

`swap_dates` checks whether **diploma date < graduation date** (i.e., the dates are in the wrong order) and swaps them if so. The correct order is diploma date ≥ graduation date (diploma is awarded after the graduation ceremony). Swap when diploma < graduation.

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

---

## Chrome Extension Lessons Learned

### executeScript world — always specify `world: 'MAIN'` when page globals are needed

`chrome.scripting.executeScript` defaults to `world: 'ISOLATED'` (the content script sandbox). ISOLATED world can read and write DOM properties but has **no access to page JavaScript globals** — `window.$find`, `Sys`, Telerik, jQuery, etc. are all undefined.

The DevTools console runs in MAIN world, so globals are visible there. This creates a false impression that page globals will also be available in injected scripts.

**Rule:** If a step needs to call any page-defined JavaScript (widget APIs, framework globals, `window.*`), that call must run in a separate synchronous `executeScript` with `world: 'MAIN'`.

**Caveat:** `world: 'MAIN'` with an `async` function does not work — Chrome does not await Promises from MAIN world scripts, so `executeScript` returns `null`. Use a **synchronous** function for any MAIN world call and pass all needed values as `args`.

> Pattern used in `swap_dates`: ISOLATED world (async) reads the DOM and returns picker IDs + date values. A second synchronous MAIN world call receives those values as `args` and calls `$find().set_selectedDate()`.

### Telerik RadDatePicker — DOM writes are ignored on save

Telerik RadDatePicker maintains its own internal JavaScript state. Writing directly to the visible text input (even using the native value setter + dispatching `input`/`change`/`blur` events) does NOT update the picker's internal state. When the form is submitted, Telerik re-syncs the hidden `_ClientState` fields from its internal state, overwriting any DOM changes.

The only reliable way to update a RadDatePicker's value before a form save is via the Telerik JavaScript API: `$find(elementId).set_selectedDate(new Date(...))`.

### FormData POST to ASP.NET WebForms — approach with extreme caution

Manually constructing a FormData POST to bypass a button click looks simple but has many failure modes with ASP.NET:

- **ViewState** encodes server-side control state; the server may restore original values from ViewState, silently ignoring POST body fields
- **Telerik controls** read from `_ClientState` JSON hidden fields, not from the text input name
- **`__EVENTTARGET`** must match the button's UniqueID exactly when the button uses `WebForm_DoPostBackWithOptions` with `clientSubmit: false` — sending an empty `__EVENTTARGET` runs Page_Load without any button handler, which can clear or reset fields
- A `200 OK` response does not mean the data was saved — it only means the server processed the request

**Lesson:** Before attempting a FormData POST, verify with a console injection what the server actually does with the values (check the response HTML for the field values). In most cases, clicking the real button via the extension is safer and simpler.

### MV3 service worker — debug log must be persisted

Chrome kills idle MV3 service workers after ~30s. Any in-memory state (including debug logs) is lost on restart. If debug events are only stored in a module-level array, every export after a run completes will show empty entries.

**Fix:** Write to `chrome.storage.session` on every `debugPush` call. On export, recover from storage if the in-memory array is empty. `chrome.storage.session` survives service worker restarts within a browser session.

### Console injection as a debugging tool

When the extension fails silently or behaves unexpectedly on a complex page, console injection into the live page is faster than adding logging and reloading the extension. Use it to:
- Verify what values fields actually hold at runtime
- Test a proposed fix (FormData POST, Telerik API call, etc.) before writing extension code
- Identify which frame context a global lives in (check the frame selector in DevTools Console)

