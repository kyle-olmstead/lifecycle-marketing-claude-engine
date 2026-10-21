---
id: lmm.deliverability.dmarc_alignment_audit
version: 2.0.0
module: 01_deliverability_and_infrastructure
type: checklist
tools: [engine/deliverability/analyze.py]
---

# DMARC Alignment Audit and Enforcement Rollout

Work top to bottom. Do not start a phase until the previous phase's exit criterion is met. Every
record is shown for the fictional domain `example.com`; substitute the real domain from
`organization` and `deliverability` in the context file. RFC 9989 (May 2026) replaced RFC 7489 as
the DMARC standard: `pct` is gone, `t=y` marks a testing phase, and `np=` sets the policy for
non-existent subdomains. During the transition some receivers still follow RFC 7489.

## Phase 0: Inventory every sender

- [ ] List every platform that sends mail as the domain or any subdomain: marketing ESP, CRM,
      sales engagement, billing and invoicing, support desk, product notifications, HR and
      recruiting, calendar tools, forms. Ask each owner; selectors cannot be discovered by lookup.
- [ ] For each platform record: From domain used, envelope (return-path) domain, DKIM signing
      domain and selector, volume, and whether it can sign with a custom domain.
- [ ] Look up each selector: `dig +short TXT <selector>._domainkey.example.com`. Key at least
      1024-bit RSA, 2048 where supported (the analyzer reports the size).
- [ ] Look up SPF at every envelope domain: `dig +short TXT example.com`. Exactly one `v=spf1`
      record, at most 10 DNS-resolving terms after expansion.

**Exit**: a sender table with no row marked "unknown". Unknown senders are where enforcement
breaks transactional mail.

## Phase 1: Publish monitoring with reporting

- [ ] Publish (or correct) the record. Host `_dmarc.example.com`, type TXT, TTL 3600:

```text
v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com
```

- [ ] The `rua` mailbox exists, or `rua` points at a DMARC report processor. A record without
      `rua` is decoration.
- [ ] Remove `pct` if present. Under RFC 9989 it is ignored, and keeping it beside `t` makes two
      populations of receivers apply two policies.

**Exit**: aggregate reports arriving daily from the major mailbox providers.

## Phase 2: Read at least two weeks of aggregate reports

- [ ] Classify every source IP and signing domain into one of four buckets:
  1. **Aligned and passing**: a known sender with aligned DKIM (or aligned SPF). Done.
  2. **Known sender, not aligned**: passes SPF or DKIM on the vendor's own domain. Fix in phase 3.
  3. **Forwarders and mailing lists**: SPF fails after forwarding; DKIM may survive. Judge on DKIM
     and ARC; do not "fix" these by loosening policy.
  4. **Unknown or spoofing**: not in the inventory. Investigate; do not authorize until identified.
- [ ] Check message volume per bucket, not just source count. One unaligned billing platform can
      carry most of the transactional volume.

**Exit**: every source in bucket 2 has an owner and a fix date.

## Phase 3: Fix every legitimate sender

- [ ] Configure custom DKIM signing on the platform with a selector on your domain (the platform
      gives the CNAME or TXT to publish). Verify with a test send: `dkim=pass header.d=example.com`.
- [ ] Configure a custom return-path (bounce) subdomain where the platform supports it, so SPF
      aligns too. Verify: `smtp.mailfrom=...@bounces.example.com` passes.
- [ ] Re-run `engine/deliverability/analyze.py analyze` on a header sample from each platform.

**Exit**: aggregate reports show every legitimate sender passing aligned DKIM (or aligned SPF) for
two consecutive weeks.

## Phase 4: Quarantine, testing first

- [ ] Publish (host `_dmarc.example.com`, TXT, TTL 3600):

```text
v=DMARC1; p=quarantine; t=y; rua=mailto:dmarc-reports@example.com
```

- [ ] Understand what `t=y` does and does not do: RFC 9989 receivers apply the policy one level
      softer (quarantine behaves like none); RFC 7489 receivers ignore `t` and quarantine in full.
      Publish this only when you could live with full quarantine.
- [ ] Watch aggregate reports and complaint and bounce rates for two weeks.

**Exit**: no legitimate mail in the quarantine dispositions for two weeks.

## Phase 5: Quarantine in full, then reject

- [ ] `v=DMARC1; p=quarantine; rua=mailto:dmarc-reports@example.com` for two clean weeks.
- [ ] Confirm transactional senders (billing, password reset, receipts) pass aligned DKIM, then:

```text
v=DMARC1; p=reject; rua=mailto:dmarc-reports@example.com
```

- [ ] Decide subdomain policy explicitly: `sp=` (existing subdomains) and `np=` (non-existent
      subdomains; `np=reject` stops spoofing of made-up subdomains).

**Exit**: `p=reject` with reports clean. BIMI becomes possible (it needs quarantine or reject); it
is cosmetic and never a reason to rush this rollout.

## Phase 6: Keep it clean

- [ ] Every new sending platform goes through phase 0 and 3 before it sends a single message.
- [ ] Monthly: read aggregate reports for new unaligned sources; quarterly: re-check key sizes and
      the SPF lookup count.
- [ ] Alert on any `dmarc=fail` volume from a known sender.
