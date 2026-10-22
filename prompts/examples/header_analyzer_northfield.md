# Worked example: header and DNS diagnosis for Northfield Outfitters (fictional)

A model answer for `prompts/jobs/deliverability/header_analyzer.md`: the analyzer's output on
`examples/deliverability/incident.json`, turned into a gate, a diagnosis, a remediation protocol and a warming plan.
Read it only when a model answer would help.

## The analyzer's input document

Every key is optional; supply what you have (`schemas/deliverability_input.schema.json`).

```json
{
  "message_class": "marketing",
  "observed_at": "2026-10-19",
  "headers": {"path": "headers_gmail.txt"},
  "dns": {
    "example.com": {
      "txt": ["v=spf1 include:_spf.esp-mail.example.net ~all"],
      "dmarc": "v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com",
      "mta_sts": "v=STSv1; id=20261019T000000",
      "tlsrpt": "v=TLSRPTv1; rua=mailto:tls-reports@example.com",
      "bimi": "v=BIMI1; l=https://example.com/brand.svg; a=",
      "mx": ["mx1.mail.example.com"]
    }
  },
  "dkim_keys": {"k1._domainkey.esp-mail.example.net": "v=DKIM1; k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDQ/ywBaTDe+pqDJWzNe3F3ZK9GFygYdqMX+IiNKL1a2Xwpe70O5Lr3bDgnlnxkYq+ewknH5kmpESWMBGgx8dAaJq4w7dXVGRlzL8p6JwA6bZRw7kRita4Ql19KjZkTYnoB4bdZpCgdz677tBokP7jsN9TOlNz6jfLMn1Td0/7vLwIDAQAB"},
  "spf_includes": {"_spf.esp-mail.example.net": "v=spf1 ip4:192.0.2.0/24 -all"},
  "mta_sts_policies": {"example.com": "version: STSv1\nmode: testing\nmx: mx1.mail.example.com\nmax_age: 604800"},
  "signals": {
    "complaint_rate": 0.0018, "hard_bounce_rate": 0.014,
    "domain_reputation_bounces_90d": 2, "domain_reputation_bounces_trend": "flat",
    "filtered_share": 0.012, "postmaster_spam_rate": 0.0021, "postmaster_compliance": "fail",
    "consumer_engagement_rate": 0.031, "enterprise_engagement_rate": 0.006, "unengaged_share": 0.46
  },
  "volume": {"daily_to_gmail": 18000, "daily_to_microsoft_consumer": 6200}
}
```

## The answer

**Input**: `examples/deliverability/incident.json`. A fictional retailer, Northfield Outfitters,
sends from `news@example.com` through an ESP. Opens at enterprise gateways collapsed after the
migration to the ESP; consumer engagement looks normal.

Header excerpt (Gmail):

```text
Authentication-Results: mx.google.com;
       dkim=pass header.i=@esp-mail.example.net header.s=k1 header.b=Qm9yZW1h;
       spf=pass (google.com: domain of bounce+7f3a@bounces.esp-mail.example.net designates 192.0.2.25 as permitted sender) smtp.mailfrom="bounce+7f3a@bounces.esp-mail.example.net";
       dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=example.com
DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed; d=esp-mail.example.net; s=k1;
        h=from:to:subject:date:message-id:list-unsubscribe;
From: Northfield Outfitters <news@example.com>
List-Unsubscribe: <mailto:unsub-7f3a@esp-mail.example.net>
```

**Analyzer verdict**: RED. Authentication is failing (DMARC fails: SPF and DKIM both pass, and
neither aligns with `example.com`), and enterprise-gateway engagement is 19% of consumer
engagement. Seven other signals are AMBER: complaint rate 0.18%, hard bounces 1.4%, two
`DOMAIN_REPUTATION` bounces in 90 days, `FILTERED` share 1.2%, Gmail spam rate 0.21%, a failing
Postmaster compliance status, and 46% of active sends to unengaged contacts.

**Diagnosis**: primary cause **identity**. The ESP signs with its shared domain and bounces through
its shared return path, so DMARC has nothing aligned to pass on. Enterprise gateways that enforce
DMARC alignment quarantine the mail; consumer mailboxes, at `p=none`, deliver it. Contributing:
**consent** (46% of active sends to unengaged contacts, and a `FILTERED` share of 1.2%, in the "present" band) and **content and infrastructure** (no
RFC 8058 one-click unsubscribe). Falsified if aligned-DKIM test sends to the same gateways still
quarantine.

**Remediation protocol** (dependency order):

| # | Owner | Change | Verify with | Done when |
|---|---|---|---|---|
| 1 | Lifecycle lead | Stop bulk sends; send only transactional mail and the engaged 30-day tier until step 5 is verified | ESP send log | no campaign to tiers older than 30 days |
| 2 | ESP admin | Enable custom DKIM signing with the selector already published at `nf1._domainkey.example.com` (2048-bit) | `dig +short TXT nf1._domainkey.example.com`; a test send shows `dkim=pass header.d=example.com` | aligned DKIM pass at Gmail, Outlook.com and one enterprise gateway |
| 3 | DNS owner + ESP admin | Custom return path: the CNAME the ESP specifies, e.g. `bounces.example.com` CNAME `bounces.esp-mail.example.net` | test send shows `smtp.mailfrom=...@bounces.example.com` | SPF passes and aligns |
| 4 | DNS owner | `_dmarc.example.com` TXT 3600 `v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com` (adds reporting; removes the obsolete `pct`) | `dig +short TXT _dmarc.example.com` | aggregate reports arriving daily |
| 5 | ESP admin | Turn on RFC 8058 headers (https `List-Unsubscribe` plus `List-Unsubscribe-Post`) and include both in the DKIM `h=` list | header inspection on a test send | analyzer reports `rfc8058_compliant: true` |
| 6 | Lifecycle lead | Suppress (do not delete) contacts with no engagement in 180 days; one re-permission send from a subdomain to the 90 to 180 day tier, expecting 1 to 5% opt-in | suppression list count | unengaged share below 20% |
| 7 | Lifecycle lead | Re-run the gate weekly. When it reads GREEN, start the warming ladder below | `lcm deliverability check example.com` | gate GREEN two weeks running |
| 8 | DNS owner | After two clean weeks of aggregate reports: `v=DMARC1; p=quarantine; t=y; rua=mailto:dmarc-reports@example.com`, then drop `t=y`, then `p=reject` | aggregate reports | every legitimate sender aligned |

**Warming schedule**: on today's RED gate the tool returns no ladder, which is the correct output.
After remediation reads GREEN, with engaged tiers of 40,000 (30 days), 70,000 (31 to 90) and
90,000 (91 to 180) at three sends per person per week:

```text
lcm warmup --target 60000 --tiers '{"engaged_30d": 40000, "engaged_90d": 70000, "engaged_180d": 90000, "unengaged": 400000}' --gate GREEN --existing-reputation --markdown
```

The ladder starts at the 30-day tier's daily capacity and doubles at each checkpoint while every
abort criterion is clear; the 400,000 unengaged contacts are never used to warm.
