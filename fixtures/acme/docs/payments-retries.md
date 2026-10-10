# Payment retries

Every charge request carries an Idempotency-Key. A retry of the same order must send the same key,
so the gateway returns the first result instead of charging again. Keys are derived from the order id
and the attempt's amount, never generated per request.

Owner: Payments squad. On-call runbook: page #payments-oncall before refunding by hand.
