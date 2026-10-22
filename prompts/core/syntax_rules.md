# LMM Engine: syntax rules

Read before writing a template, copy, SQL, a JSON payload or a DNS record. Zero tolerance: a deliverable that
breaks one of these is not finished. The engine checks most of them: `lcm lint` (templates), `lcm sql-check` (SQL)
and `lcm sms` (SMS encoding and segments).

## Templates

- Name the dialect on every template block, in the fence info string: `liquid braze`, `liquid customerio`,
  `liquid customerio_legacy`, `django klaviyo`, `hubl`, `ampscript`. Only Braze and Customer.io use Liquid. Klaviyo
  uses a Django-style language, HubSpot uses HubL, Salesforce Marketing Cloud uses AMPscript; never call those three
  "Liquid".
- Every opening tag has its closing tag: Liquid `if/endif`, `unless/endunless`, `for/endfor`, `case/endcase`,
  `capture/endcapture`; Klaviyo and HubL `if/endif`, `for/endfor`; AMPscript `IF/ENDIF`, `FOR/NEXT`, `%%[ ]%%`,
  `%%= =%%`.
- Platform tags are spelled exactly: Braze `{% connected_content <url> :save var %}` and
  `{% abort_message('reason') %}`; Customer.io `{% unsubscribe_url %}`; Klaviyo `{% unsubscribe %}`; HubL
  `{{ unsubscribe_link }}`; AMPscript `%%unsub_center_url%%`.

## The fallback rule

This is the one statement of it; job prompts and skills point here.

Every personalization value that can be missing is output either with an explicit fallback in the platform's
dialect, or inside an `if` guard on that same value whose `else` branch reads correctly without it. "Hi ," is worse
than no personalization. A fallback is neutral copy that fits the sentence ("there", "your order"), never an invented
value. No fallback is needed for string literals, loop variables, variables assigned in the template, Braze system
attributes (`user_id`, `braze_id`, `email_footer`, the subscription URLs) and Klaviyo `organization` fields.

- Customer.io (latest Liquid): `{{ customer.first_name | default: "there" }}`
- Customer.io legacy Liquid has no `default` filter (the linter rejects one): guard instead,
  `{% if customer.first_name != blank %}Hi {{ customer.first_name }},{% else %}Hi there,{% endif %}`
- Braze: `{{ ${first_name} | default: 'there' }}`
- Klaviyo: `{{ first_name|default:'there' }}`
- HubSpot HubL, for contact and company fields: `{{ personalization_token("contact.firstname", "there") }}`
- Salesforce Marketing Cloud AMPscript: `IF Empty(@fn) THEN SET @fn = "there" ENDIF`, then `%%=v(@fn)=%%`

`lcm lint --dialect <dialect> FILE` warns on every output that has neither.

## SQL

- Line one is `-- dialect: snowflake | bigquery | postgresql`; line two names the transaction object. Never guess
  the dialect.
- The as-of date and every window live in a `params` CTE. `CURRENT_DATE` scattered through a query makes it
  impossible to reproduce for an audit.
- Use the dialect's own functions (`docs/SQL_SCHEMA_CONVENTIONS.md` section 6). BigQuery has no aggregate
  `PERCENTILE_CONT`; Snowflake writes `DATEDIFF('day', a, b)`; PostgreSQL subtracts dates.
- Window functions are computed in one CTE and filtered in the next. Final outputs name their columns; no `SELECT *`
  in a deliverable.
- `NTILE` splits ties arbitrarily. Use it on continuous measures with a deterministic tiebreaker; band discrete
  measures (order counts) instead.

## JSON, payloads and webhooks

- JSON is valid (double quotes, no comments, no trailing commas) and validates against the schema the job names.
- An API payload states the endpoint, method, required headers (credentials as environment variable names, never
  literal keys), the body, the vendor's batch and size limits, an idempotency key, and the retry policy (honor
  `Retry-After` on 429; exponential backoff with jitter; a dead-letter destination after the last retry).
- API versions and limits change. State the date the limits were read and tell the user to check them before a
  production run.

## DNS

Every record as host, type, TTL and exact value. Never a description of what a record should contain.
