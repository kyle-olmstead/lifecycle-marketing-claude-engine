---
name: Deliverability incident
about: Inbox placement dropped, a provider started bulking or rejecting, or the analyzer disagrees with what you observe
title: "[deliverability] "
labels: [deliverability, incident]
---

<!-- Redact recipient addresses. Sending domains, DNS records and headers are needed; keep them. -->

## Gate

Run the analyzer and paste the first section (the gate and its signals):

```text
python3 engine/deliverability/analyze.py analyze incident.json --markdown
```

## Symptom

- **What changed, and when** (date of the first bad send):
- **Which providers** (Gmail, Outlook.com, Yahoo, enterprise gateways), with engagement by provider
  (clicks or bot-excluded opens, not raw opens):
- **Bounce and rejection text, verbatim** (for example `550 5.7.515` from Outlook.com):
- **Gmail Postmaster**: spam rate and compliance status for the period:

## Evidence

- [ ] Raw headers from two or more affected messages, from different providers
- [ ] `dig` output for SPF, DKIM (each selector), DMARC, MTA-STS and TLS-RPT, with the date it was run
- [ ] The full sending inventory: every platform that sends as the domain
- [ ] Complaint and hard-bounce rates for the last 30 days, and DOMAIN_REPUTATION or FILTERED bounce counts
- [ ] List changes in the period: imports, re-activated segments, new sources

## What the analyzer got wrong (if anything)

<!-- A parsed fact that does not match the evidence is a bug in engine/deliverability/analyze.py. Include the input that reproduces it. -->
