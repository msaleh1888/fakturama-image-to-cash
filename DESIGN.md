# Fakturama Image-to-Cash Automation — Design

## 1. Objective

The system converts one order image into two verified accounting documents in Fakturama:

1. a saved Order containing the source customer, dates, items, discounts, VAT, and totals; and
2. a saved paid Invoice created as a follow-up document from that Order.

The required output is not merely a successful sequence of clicks. The implementation must prove that the extracted data is financially consistent, that Fakturama accepted the intended values, and that the saved Order and Invoice are uniquely persisted and linked.

The implementation is a synchronous Windows CLI for Fakturama 2.2.0 with an English UI. It processes one order per invocation.

## 2. Supported scope

The submission deliberately implements one complete, repeatable happy path:

- USD orders using the prepared US-region workspace;
- Northstar Office Inc. with one identical billing and delivery address;
- Bank Transfer;
- VAT 19%;
- the prepared products `CHR-ERG-01` and `MAT-DESK-02`;
- line-level quantity, unit net price, discount, VAT, and line net values;
- a paid linked Invoice with the source payment date and full gross value; and
- Fakturama 2.2.0 on an unlocked, visible Windows desktop.

Five synthetic US/USD image fixtures exercise different references, dates, quantities, prices, discounts, and totals while retaining the same prepared master records. All five have passed the complete image-to-persisted-Invoice workflow.

Missing or ambiguous master data fails safely. Automatic creation of Debtors, payment methods, VAT definitions, or Products is not implemented. Alternate currencies, different billing and delivery addresses, batch processing, rollback, resumability, and other Fakturama versions or languages are outside the supported boundary.

## 3. System shape

```text
image
  -> OpenAI Responses API structured extraction
  -> OrderData parsing and deterministic validation
  -> reviewed JSON
  -> fresh copy of the prepared Fakturama workspace
  -> Fakturama Order entered and verified
  -> linked Invoice entered, paid, and verified
  -> final UI and read-only persistence verification
```

The system has four application modules:

- `main.py` owns CLI parsing, environment loading, JSON input/output, workspace preparation, and orchestration boundaries.
- `models.py` defines the shared `OrderData` contract and deterministic validation.
- `extract.py` owns image preparation and the single OpenAI extraction request.
- `fakturama.py` owns Fakturama interaction and verification.

`OrderData` is the only business-data object shared between extraction and automation. There is no server, queue, application database, repository layer, dependency-injection framework, agent loop, or generalized workflow engine.

## 4. Extraction boundary

The extraction adapter uses the OpenAI Responses API with Structured Outputs. It sends one image and parses the response directly into `OrderData`; it does not first create untyped OCR text and then infer a schema locally.

The extraction instructions require the model to:

- use only values visible in the image;
- preserve references, identifiers, names, SKUs, spelling, and item order;
- return null for missing or unreadable values;
- keep unit price, discount, VAT, line net, and document totals as separate source values; and
- return dates and numeric fields in the contract's required formats.

Expected fixture answers are not included in the prompt. This prevents the extraction layer from filling unreadable values with known test data.

PNG, JPEG, and WEBP inputs are accepted. Images larger than 250 KB are downscaled in memory to fit within 700 × 900 pixels and encoded as optimized JPEG. The source file is never modified.

## 5. Domain contract and financial validation

`OrderData` contains:

- external reference, order date, and currency;
- Debtor identity, contact fields, billing address, and delivery address;
- payment method, paid status, and payment date;
- ordered items with SKU, description, quantity, unit, unit net, discount, VAT, and visible line net; and
- source net, VAT, and gross totals.

Dates are typed dates. Quantities, percentages, prices, and totals use `Decimal`, never binary floating point. Monetary results use two-decimal `ROUND_HALF_UP` rounding.

For each item, validation calculates:

```text
raw line net = quantity × unit net × (1 − discount / 100)
```

The rounded line net must equal the value visible in the source. Document totals are derived from the raw discounted line values:

```text
net   = round(sum(raw line net))
VAT   = round(sum(raw line net × line VAT / 100))
gross = round(sum(raw line net + raw line VAT))
```

Validation also requires all happy-path fields, positive quantities, nonnegative money, percentages between 0 and 100, a payment date for paid orders, no payment date for unpaid orders, and no payment date before the Order date.

An invalid model stops before reviewed JSON is written and before Fakturama is launched.

## 6. Workspace and configuration design

The repository contains a prepared Fakturama workspace with only the required active Debtor, address roles, payment method, VAT rate, Products, and US/USD preferences. It contains no saved Orders or Invoices.

Both `automate` and `run`:

1. validate the Fakturama executable;
2. copy the prepared workspace into a unique directory under `.runtime/fakturama`;
3. launch Fakturama with the exact `--workspace <runtime-path>` argument; and
4. match the main window against that same resolved path.

The source fixture is never opened as the live workspace. Runtime copies remain after success or failure so a transaction can be diagnosed without contaminating the next run.

The default executable is `C:\Program Files\Fakturama2\Fakturama.exe`; both automation commands accept an explicit override. `OPENAI_API_KEY` is required only for image extraction. Diagnostic output defaults to `diagnostics/`.

## 7. Fakturama grounding strategy

Fakturama is an SWT application. Standard controls are located primarily through Microsoft UI Automation using semantic evidence:

- exact accessible name or title;
- control type and SWT/Win32 class;
- stable semantic parent;
- visible and enabled state; and
- required uniqueness.

Process IDs, native handles, UIA AutomationIds, and absolute screen coordinates are never stored as locators because they change between launches.

Some owner-drawn tables do not expose rows and cells through UIA. The implementation uses narrow, evidence-based fallbacks:

- selector and Documents searches receive Win32 character messages after their Edit control is located semantically, because ordinary value assignment does not notify SWT's filter listener;
- selector results are rendered from the current result pane and selected only after exact persistence identity and one rendered row are proven;
- item-grid row and column boundaries are derived from a fresh rendering of the current table, scaled to the live control dimensions;
- a calculated cell point is used to open Fakturama's in-place Edit, and the committed value is read back; and
- date controls receive one complete English-formatted date paste followed by exact readback, avoiding unreliable segmented date-field selection.

These techniques operate relative to the currently located control. They do not use recorded desktop positions.

## 8. Exact master-record resolution

The prepared workspace contains one supported Debtor and two supported Products. Before selection, read-only persistence inspection proves the exact active identity:

- Debtor company, contact, address, country, billing role, and delivery role;
- Product SKU, description, and VAT identity;
- payment-method name; and
- VAT percentage and active record.

The live selector is filtered using the exact company or SKU. A row is selected only when persistence proves one matching primary key and the current selector state proves one corresponding result. Zero matches, multiple matches, deleted matches, or conflicting values stop the run; there is no fuzzy selection.

## 9. Order-first transaction

The automation performs one straight-line transaction:

```text
connect to the isolated workspace
  -> open New Order
  -> verify USD display
  -> enter date and external reference
  -> select Net pricing and retain VAT
  -> select and verify the exact Debtor and both addresses
  -> select each Product in source order
  -> set and read back quantity, unit price, VAT, and discount
  -> verify both complete rows, charges, net, VAT, and gross
  -> save the Order once
  -> verify the generated Order and Documents row
  -> create the Invoice from the saved Order's follow-up action
  -> verify inherited reference, Order date, addresses, rows, charges, and totals
  -> apply payment method, paid state, payment date, and full gross value
  -> save the Invoice once
  -> verify both documents, receivers, payment, states, and reciprocal link
```

The Invoice is never created from the top-level New Invoice action because that would not prove the required Order relationship.

## 10. Verification and persistence

Each meaningful interaction follows the same pattern:

1. verify the starting state;
2. locate exactly one intended control;
3. perform one action;
4. wait for its observable postcondition; and
5. read back the accepted value.

Before either Save, the implementation verifies all information accessible in the UI. After Save, it also reads Fakturama's HSQLDB `Database.script` and active `Database.log` without modifying either file. Persistence inspection proves identities and relationships that owner-drawn UI controls do not expose reliably, including:

- unique generated document numbers;
- external reference and totals;
- exact receiver fields and US country;
- open Order state;
- paid Invoice state, payment date, and value; and
- reciprocal Order/Invoice relationship fields.

Fakturama normally omits the home country from rendered address text, so the visible address verifies recipient, street, and ZIP/city while persistence verifies the exact `US` country.

## 11. Save and failure semantics

The Order Save and Invoice Save are each invoked once. If a post-Save result is ambiguous, the shared guard inspects the disabled Save state, saved editor, Documents view, and persistence. It does not issue another Save while the first result is unknown.

State waits are bounded and report their last observed value instead of hanging indefinitely. A failure after connecting captures one screenshot and UIA tree while preserving the original exception. Optional detailed date and grid traces can be enabled with `FAKTURAMA_DETAILED_TRACE=true`; diagnostic failures never replace the business failure.

Runtime workspaces are retained after failure. The system does not automatically retry financial actions, rewrite extracted business data, roll back a partial transaction, or resume one.

## 12. Testing and acceptance

The deterministic test suite covers:

- model validation, financial calculations, serialization, and fixture consistency;
- image encoding and in-memory downscaling;
- CLI parsing, executable validation, workspace isolation, and object handoff;
- exact persistence matching and ambiguity rejection;
- selector, item-grid, Documents-grid, date-entry, and payment interactions;
- one-save ambiguity handling and diagnostic preservation; and
- prepared-workspace integrity, including the absence of saved documents and lock/log files.

Deterministic tests mock external OpenAI and Fakturama boundaries. They do not claim desktop acceptance. The real acceptance test is `run`, which performs extraction, validation, isolated workspace creation, Fakturama entry, Save, and persistence verification. All five supplied image fixtures have completed that path successfully.

## 13. Tradeoffs

Structured vision extraction is the shortest adaptable solution for labelled order documents, but it adds a network dependency, request cost, and possible transcription error. A strict typed schema plus independent Decimal validation reduces—not eliminates—that risk, so the reviewed JSON remains a visible artifact.

Desktop UI automation is more sensitive to application state than a supported API. Semantic control grounding, live-render geometry, exact matching, state-based waits, readback, and read-only persistence verification make the narrow supported path defensible without claiming general Fakturama automation.

Prepared master data is the principal scope tradeoff. It omits conditional creation branches but permits a complete, verified image-to-cash transaction instead of several partially proven master editors. The design favors one reliable vertical path over speculative framework code.
