# SMS templates

The engine writes texts in words; the renderer (engine/agents/copywriter.py, `render_short`) turns them into what is
sent, the same way on every platform:

1. **Brand first.** The text opens with the product or brand name ("Acme: ..."), unless the words already do, so the
   recipient knows who is texting before the first word.
2. **The way back.** Cart and browse texts end with the link to that checkout or product; other texts end with the
   brief's `cta_url` where the words ask for `{link}`. The platform shortens the link (Klaviyo, Braze and Customer.io
   all do on send), so segments are counted with a 21-character short link.
3. **Opt-out.** "Reply STOP to opt out." is added unless the words already carry an opt-out instruction
   (`engine/render/sms.py has_opt_out`, which needs an instruction, not just the word).
4. **One segment.** QA counts GSM-7 and UCS-2 units on the worst-case rendering, with escape characters and emoji that
   cannot straddle a segment boundary (`lcm sms "<text>"` runs the same count). An emoji switches the whole text to UCS-2
   and 70 characters a segment.
5. **When.** Inside the SMS permitted window, 09:00 to 20:00 recipient local time unless `lmm.yaml` narrows it, and
   never more than the workspace's SMS cap (one a day in the template).

The starter texts the offline library writes, rendered and counted the way QA counts them (generated from the code by
`python3 -m engine.docs_gen --write`):

<!-- generated: sms -->
| Flow | Touch | Text as sent (preview, with a shortened link) | Encoding | Segments |
|---|---|---|---|---:|
| cart_abandon | t2_sms A | Acme: Questions about shipping or returns? Reply here and we will answer. https://ex.co/a1B2c3d Reply STOP to opt out. | GSM-7 | 1 |
| cart_abandon | t2_sms B | Acme: Not sure it is the right one? Reply with your question and a person will answer. https://ex.co/a1B2c3d Reply STOP to opt out. | GSM-7 | 1 |
| winback | t4_sms A | Acme: People who stay with Acme use the core feature on the same day each week. Start yours. https://ex.co/a1B2c3d Reply STOP to opt out. | GSM-7 | 1 |
| winback | t4_sms B | Acme: Ten minutes a week keeps Acme working for you. https://ex.co/a1B2c3d Reply STOP to opt out. | GSM-7 | 1 |
| activation | t5_sms A | Acme: Still want to try Acme? Your first step: use reports. https://ex.co/a1B2c3d Reply STOP to opt out. | GSM-7 | 1 |
| activation | t5_sms B | Acme: No rush. When you are ready, Acme starts here. https://ex.co/a1B2c3d Reply STOP to opt out. | GSM-7 | 1 |
<!-- end generated -->

Platform syntax for personalization, links and opt-out lives in each platform's dialect
(`engine/render/template_lint.py`); `lcm lint --dialect <dialect>` checks a text you wrote yourself.
