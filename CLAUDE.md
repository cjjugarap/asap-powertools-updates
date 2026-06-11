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

### Mapping Protocol (going forward)

For each step in a process being mapped:
- Run the inspector **before** the action to capture the current state.
- Perform the action.
- Run the inspector **again** to capture any newly-revealed elements.
- Explicitly ask: "What did this action produce?" (new elements, navigation, download, dialog?)

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
