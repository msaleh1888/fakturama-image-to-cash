# Fakturama Image-to-Cash Automation - Design

## Objective and scope

The system accepts one scanned or photographed order and turns it into a saved Fakturama Order and a linked Invoice. It extracts the source data, validates the financial relationships, resolves the required master records, enters the transaction through Fakturama's UI, applies the payment status, and verifies each important transition before continuing.

The design covers the complete workflow requested by the assignment, including conditional creation of missing Debtors, payment methods, VAT rates, and Products. The timeboxed implementation deliberately targets one simpler happy path: Fakturama 2.2.0 with an English UI and a prepared disposable workspace containing the exact required master records. If one of those records is missing or ambiguous, the submitted implementation stops rather than entering an untested creation branch. This limitation is explicit in the README.

The solution is a synchronous Python command-line application. It handles one order at a time and does not introduce a server, queue, application database, workflow engine, or generalized desktop-automation framework.

## System shape

The pipeline has one shared typed boundary, `OrderData`:

```text
order image
  -> one vision extraction request
  -> structured OrderData
  -> deterministic validation
  -> reviewed JSON
  -> Fakturama UI automation
  -> verified Order
  -> linked, verified Invoice
```

The command-line interface exposes three paths:

- `extract` converts an image to validated reviewed JSON.
- `automate` loads reviewed JSON and performs the Fakturama workflow.
- `run` performs extraction and then passes the same validated `OrderData` to automation.

Separating extraction from automation makes failures easier to locate and allows the desktop flow to be developed and demonstrated without another API request. Both paths use the same model and validation rules, so reviewed JSON cannot bypass the financial checks.

## Image extraction and validation

The extraction stage uses an OpenAI vision model through the Responses API with Structured Outputs. The local image is sent once and parsed directly into a Pydantic `OrderData` model. This is simpler for the supplied document than combining traditional OCR with layout-specific parsing rules for labelled sections, addresses, payment information, and an item table.

The extraction instruction is source-grounded: extract only visible values, preserve identifiers and item order, and return null for absent or unreadable fields. Expected fixture answers are not included in the prompt. This prevents the extraction request from silently replacing unreadable source content with known test values.

`OrderData` contains:

- order date, external reference, and currency;
- Debtor company, contact, alias, email, phone, billing address, and delivery address;
- payment method, paid status, and payment date;
- every item SKU, description, quantity, unit, unit net price, discount, VAT percentage, and visible line net; and
- visible net, VAT, and gross totals.

Dates are typed dates and serialize as ISO values. Quantities, percentages, and money use `Decimal`, never binary floating point. Before any UI interaction, deterministic validation requires every happy-path field and recalculates each line using two-decimal `ROUND_HALF_UP` rounding:

```text
line net = quantity * unit net * (1 - discount / 100)
line VAT = line net * VAT / 100
```

Calculated line nets must match the source line nets. Their summed net, VAT, and gross values must match the source document totals. Invalid, incomplete, or inconsistent data stops before Fakturama is touched.

## Fakturama grounding strategy

The automation targets Fakturama 2.2.0 on Windows and primarily uses Microsoft UI Automation through `pywinauto`. The application is located by its exact configured workspace title, SWT window class, visibility, and uniqueness. Process IDs, window handles, AutomationIds, and screen coordinates are not stored because they change between launches.

Normal controls are grounded by semantic context: the active editor, exact accessible name, control type, stable parent, visible state, and expected match count. This covers toolbar actions, labelled fields, ComboBoxes, dialog buttons, and the saved Order's follow-up Invoice action.

Fakturama uses SWT owner-drawn controls that do not expose every row and cell through UIA. The narrow fallbacks are:

- selector searches use Win32 character messages on the semantically located Edit because UIA value assignment does not trigger SWT's filter listener;
- selector results are rendered from the currently located result Pane and selected relative to its current row geometry;
- item-grid rows and columns are derived from a fresh render of the current Items table, then edited through the in-place Edit that appears after a runtime-relative double-click; and
- Documents rows are corroborated through current rendering and read-only persistence inspection.

These fallbacks use current control bounds, not fixed desktop positions or dimensions captured during development.

Every interaction follows the same rule:

1. verify the expected starting state;
2. locate one intended control;
3. perform the action;
4. wait for an observable state change; and
5. read back the resulting value before continuing.

Waits are state-based rather than arbitrary delays. A timeout captures a screenshot and relevant UIA state for diagnosis.

## Master-record resolution

The Order's Debtor and Product selectors are the existence checks required by the assignment. A Debtor is an exact match only when company, contact name, ZIP, and city agree with the source. A Product is an exact match only when its SKU agrees, with its expected description and VAT corroborated where the owner-drawn selector truncates values. Read-only Fakturama persistence data can prove exact identity when the UI does not expose complete row text. Zero, multiple, or conflicting exact matches never result in a fuzzy selection.

In the complete target design, zero matches enter a controlled creation branch while the New Order remains open. A missing Debtor is created with Fakturama's proposed customer ID, source contact and addresses, Net mode, alias, and exact payment method. A missing payment method is created from the specified name-to-code mapping. Before a missing Product is created, its exact VAT definition is resolved or created; the Product then receives the source SKU and description, calculated gross master price, and exact VAT. Each created record is saved once and must become selectable from the still-open Order before the workflow continues.

The submitted happy-path implementation does not execute these creation branches. Its prepared workspace already contains Northstar Office GmbH, Bank Transfer, VAT 19%, `CHR-ERG-01`, and `MAT-DESK-02`. This keeps the implementation focused on the complete Order-to-Invoice transaction rather than partially proving several master-data editors.

## Order-first transaction

The automation opens a New Order first, leaves Fakturama's proposed number unchanged, enters the source date and external reference, selects Net pricing, and keeps VAT enabled. It selects the exact prepared Debtor from the Order and verifies the populated billing and delivery addresses.

For every source item, in source order, it selects the exact Product and edits the current row's quantity, unit net price, VAT, and discount. The owner-drawn grid geometry is recalculated from the current rendering each time. The committed value is read back, and the calculated line Price is checked before continuing.

Before Save, the automation verifies both addresses, item order and values, zero order-level discount and shipping when absent from the source, and Total Net, VAT, and Total. Only then does it invoke Save. The generated `PO...` number, exact reference, total, and unique persisted identity are verified before Invoice creation.

The Invoice is created only from the saved Order's `Create a follow-up document > Invoice` action. The top-toolbar New Invoice action is not used because it would not guarantee the required relationship. In the linked Invoice, the automation verifies the copied reference, addresses, Order Date, item lines, VAT mode, and totals. It selects the source payment method and, for a paid source, sets paid state, the extracted payment date, and Value equal to the full Invoice total.

After Save, final verification requires one generated `INV...` number, the expected paid values, and one unique Order and Invoice connected by reciprocal relationship fields. The application never writes Fakturama database files directly; `Database.script` and `Database.log` are read only where UI verification is insufficient.

## Failure handling and evidence

Input, validation, exact-match, control, timeout, readback, and persistence failures produce a concise nonzero command exit. When an operation fails, diagnostics are used to identify and fix the failed locator, input, timing, or state assumption.

Financial Save actions require special care. If a post-Save state is unclear, the system inspects the active editor, Documents view, and persistence before deciding whether Save succeeded. It does not issue another Save while the result of the first action is unknown, which avoids duplicate financial records.

Assessment evidence consists of the source image, validated JSON, generated document numbers, and a small set of annotated screenshots or a recording showing the completed Order, linked paid Invoice, and final verification.

## Tradeoffs

LLM vision is the shortest adaptable extraction approach for this timebox, but it adds a network dependency, per-call cost, and the risk of plausible transcription mistakes. Structured output constrains the shape, while independent Decimal validation catches financial inconsistencies. Keeping the source image and reviewed JSON allows human inspection.

UI automation is inherently more sensitive to application state than a supported API. Semantic grounding, exact-match rules, current-render geometry, state-based waits, readback, and read-only persistence verification reduce that risk without pretending to support every Fakturama version or layout.

The main implementation tradeoff is prepared master data. It omits part of the complete target workflow, but it allows the submitted code to pursue one meaningful continuous happy path: validated image data to a saved Order and its linked paid Invoice. Missing-master creation, ambiguity review, interruption recovery, batch processing, alternate Fakturama versions, and alternate UI languages remain explicit limitations rather than speculative abstractions.
