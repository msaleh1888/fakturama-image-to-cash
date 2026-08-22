# End-to-End Acceptance Evidence

Acceptance was completed on August 22, 2026, using Windows, Python, Fakturama 2.2.0 with its English UI, and the repository's prepared US/USD workspace fixture.

## End-to-end results

Each image was executed with the complete `run` command:

```powershell
python main.py run samples\synthetic_order_XX.png --output output\synthetic_order_XX.json
```

Every invocation performed a real OpenAI structured extraction, validated the resulting `OrderData`, created a fresh isolated Fakturama workspace, saved an Order, created and saved its linked paid Invoice, and completed UI and read-only persistence verification.

| Fixture | External reference | Currency | Gross | Extracted JSON matches expected | End to end |
| --- | --- | --- | ---: | --- | --- |
| `synthetic_order_01` | `WEB-2026-0714-A17` | USD | $678.30 | Yes | Passed |
| `synthetic_order_02` | `WEB-2026-0721-B04` | USD | $505.75 | Yes | Passed |
| `synthetic_order_03` | `WEB-2026-0803-C12` | USD | $819.99 | Yes | Passed |
| `synthetic_order_04` | `WEB-2026-0817-D09` | USD | $764.58 | Yes | Passed |
| `synthetic_order_05` | `WEB-2026-0902-E21` | USD | $700.28 | Yes | Passed |

The structural comparison loaded both the extracted and expected JSON through the same schema and compared their complete JSON structures.

## Captured Fakturama evidence

The included screenshots are from the successful `synthetic_order_02` run:

- [`verified-item-rows.png`](verified-item-rows.png) shows both USD Invoice rows with the expected quantities, SKUs, VAT, unit prices, discounts, and calculated prices.
- [`final-order.png`](final-order.png) shows verified Order `PO000006` in the open state with the fixture reference.
- [`final-invoice.png`](final-invoice.png) shows linked Invoice `INV000005` in the paid state with the same fixture reference.

The automation additionally verified the exact Debtor and receiver fields, identical billing and delivery addresses, document totals, payment method, payment date, paid value, open Order state, absence of forbidden follow-up documents, and reciprocal Order/Invoice persistence relationship.

## Deterministic verification

```powershell
python -m compileall -q main.py models.py extract.py fakturama.py tests
python -m unittest discover -s tests
python -m pip check
git diff --check
```

Results:

- compilation passed;
- 53 tests passed;
- dependency consistency passed with no broken requirements;
- diff whitespace checks passed; and
- the intended submission files contain no OpenAI API key.

The deterministic tests do not call OpenAI or control Fakturama. The five explicit `run` executions above provide the real integration evidence.
