# Delivery test log (MOCKED)

**No payment took place.** Foundry contains no payment code. This is a simulated run of the delivery rules only: the order, the buyer and the access token are all `MOCKED`.

- Project: `delivery-test`
- Product: Sample digital product
- Simulated order id: `MOCK-ORDER-A4336A16`
- Run at: 2026-11-17T13:09:10.354159+00:00

| Check | Result |
|---|---|
| First order event delivers once | PASS |
| Repeated event for the same order does not deliver again | PASS |
| Buyer has access after delivery | PASS |
| Refund event revokes access | PASS |
| Order event after a refund does not restore access | PASS |

Result: 5/5 checks passed.
