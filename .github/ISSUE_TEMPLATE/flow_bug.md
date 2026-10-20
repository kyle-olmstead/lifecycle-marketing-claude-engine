---
name: Flow bug
about: A flow spec, template or segment from the engine misfired, double-sent, or did not fire
title: "[flow] "
labels: [flow, bug]
---

<!-- Remove customer data before posting: no email addresses, names or IDs from real people. -->

## What happened

- **Flow and step** (flow name and step id from the spec):
- **Platform and template dialect** (customerio, customerio_legacy, braze, klaviyo, hubl, ampscript):
- **Symptom** (did not fire, fired twice, wrong people, rendered "Hi ,", wrong branch, other):
- **When it started and how many people it affected** (labeled LIVE PULL with a date, or ESTIMATE):

## The spec the engine produced

Paste the flow spec JSON (or attach it). Does it validate?

```text
python3 -c "import json,jsonschema; jsonschema.validate(json.load(open('flow.json')), json.load(open('schemas/flow_spec.schema.json')))"
```

## Checks

- [ ] Each filter in the platform is set as the spec says: entry only versus every step (the most
      common build error on every platform)
- [ ] Re-entry or re-enrollment is set as the spec says
- [ ] The trigger event and every branch field exist and are filled at the rates the spec assumed
- [ ] Suppressions are attached (unsubscribed, hard bounced, conflicting flows)
- [ ] Templates pass `python3 tools/template_lint.py --dialect <dialect> <file>`
- [ ] The holdout assignment property is set on both arms

## Expected versus actual

<!-- What the spec says should happen, and what the platform's logs show did happen. -->
