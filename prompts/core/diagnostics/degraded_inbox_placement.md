# Diagnostic: degraded inbox placement

Read when open or click rates fell, or a mailbox provider started bulking or rejecting. Work the causes in this
order; stop at the first that explains the size of the drop.

1. **Measurement**: did the metric definition, bot filtering or attribution window change? A step change on one
   date is almost always this.
2. **Deliverability**: run the gate (`lcm deliverability check <domain>`, or `lcm deliverability analyze` on
   collected evidence) and split engagement by mailbox provider; enterprise gateways far below consumer mailboxes
   means quarantine, not disinterest. Anything found here goes to
   `prompts/jobs/deliverability/header_analyzer.md`.
3. **List composition**: did the denominator change (an import, a re-activated segment)?
4. **Data integrity**: did a sync or field break so the wrong people were mailed?
5. **Coverage**: did a flow stop firing (`lcm daily`, `lcm audit`)?
6. **Creative and offer**: last, not first.
