# Brand kit

> **In plain English.** A brand kit is one small file, `brand.yaml`, that tells the engine what your emails look
> like: your logo, your colors in light and dark mode, your fonts and your footer. It also tells the writer how
> your brand sounds and who emails come from. Every email the engine renders uses it, on every platform. With no
> kit, emails use a neutral default: a white card, near-black Arial text and a dark green button.

## 1. Where it lives

Put `brand.yaml` in your workspace folder, next to `lmm.yaml`. To keep it somewhere else, name it in `lmm.yaml`:

```yaml
brand_kit: kits/brand.yaml   # relative to the workspace folder
```

Start from the commented template, `templates/brand/brand.yaml`. Two filled examples show a B2B product and a
consumer brand: `examples/brands/ridgeview_saas/brand.yaml` and `examples/brands/northfield_dtc/brand.yaml`.

## 2. What to fill in

Only `brand_name` is required. Anything you leave out comes from the neutral default.

| Section | What it is | Tips |
|---|---|---|
| `brand_name` | Your name as readers know it | Shown in the footer's sender line |
| `lang` | The language tag, such as `en` or `de` | Screen readers use it to pick a voice |
| `website` | Your home page | The logo links here |
| `logo` | `url`, `alt`, `width`, optional `height` | PNG or JPG at twice the display width; no SVG; alt text is required |
| `colors` | `primary` (button), `button_text`, `text`, `background`, `card`, `link`, and the same under `dark` | Hex colors like `#2B4C7E`; checked for contrast (section 3) |
| `fonts` | A web-safe `stack`, optional `web_font` (`name`, `url`) | Outlook for Windows always uses the first font in the stack |
| `footer` | `legal_name`, `postal_address`, up to four `extra_lines` | The unsubscribe link is always added for you |
| `voice` | `register`, `vocabulary` (avoid / use instead), `forbidden_phrases`, `sign_off` | Given to the writer and checked after drafting; never printed |
| `senders` | `marketing` and `person`: `from_name`, `from_address`, `reply_to` | Use domains you have authenticated |
| `social` | Up to six `label` and `url` pairs | Shown as text links, so nothing depends on images |

Text fields take words only: no HTML and no template code. The engine writes each platform's own code (the
first-name fallback, the unsubscribe link, the address token) for you.

The postal address: Klaviyo, HubSpot and Salesforce Marketing Cloud print the address from your account settings.
For Customer.io and Braze the address comes from the brief, or from the kit's `footer.postal_address` when the brief
has none.

## 3. How contrast is checked

Every text color is measured against what it sits on, in light mode and in dark mode:

| What is read | Foreground | Background |
|---|---|---|
| Body text | `text` | `card` |
| Footer text | `text` | `background` |
| Button label | `button_text` | `primary` |
| Links in the body and the footer | `link` | `card` and `background` |

Each pair needs a contrast ratio of at least **4.5:1**, the WCAG 2.1 level AA minimum for normal-size text
(Success Criterion 1.4.3, [Contrast (Minimum)](https://www.w3.org/TR/WCAG21/#contrast-minimum)). The ratio is
`(L1 + 0.05) / (L2 + 0.05)`, with L1 and L2 the
[relative luminance](https://www.w3.org/TR/WCAG21/#dfn-relative-luminance) of the lighter and darker color, so it
runs from 1:1 (no contrast) to 21:1 (black on white). Body and button text in these emails is 16px, which counts as
normal text, so the 4.5:1 rule applies to all of it. A kit with any pair below 4.5:1 is rejected, and the message
names the pair, both colors and the ratio, so you know which one to change.

The button's fill against the card is also measured against the 3:1 non-text minimum
([SC 1.4.11](https://www.w3.org/TR/WCAG21/#non-text-contrast)) and shown in the table, but it does not reject the
kit: the label already tells readers what the button is.

Missing colors are filled in so they stay readable: a missing `link` uses `primary` when it passes on the card and
background, otherwise `text`; missing dark colors use `#F2F2F2` text on a `#1E1E1E` card over `#121212`, keep the
light button colors, and use the light link only when it is readable on the dark card.

## 4. Check and preview

```text
python3 engine/render/brand.py brand.yaml
```

prints the contrast table and exits with an error if the kit is not usable. To see an email built from the kit:

```text
python3 engine/render/mjml_render.py --brand brand.yaml --dialect klaviyo
python3 engine/render/mjml_render.py --brand brand.yaml --dialect all --out-dir previews
```

Each writes `lcm_preview_<dialect>.html` (open it in a browser), the `.mjml` source it was compiled from and the
plain-text version, and prints the template linter's verdict. The dialects are `customerio`, `braze`, `klaviyo`,
`hubl` (HubSpot) and `ampscript` (Salesforce Marketing Cloud). These become `lcm brand check` and
`lcm render --brand brand.yaml --dialect klaviyo`.

## 5. What the renderer does with it

Emails are written in [MJML](https://mjml.io) (the components are in `templates/email/mjml/`) and compiled with
the `mjml` Python package, which produces the table layout, the Outlook conditional comments that hold the layout in
Outlook for Windows, and the media queries that stack the layout on phones. Dark-mode colors apply in clients that
support `prefers-color-scheme` (Apple Mail, iOS Mail, Outlook for Mac) and in Outlook.com. The Gmail apps choose
their own dark-mode colors whatever the email says. Every email keeps a hidden preheader, an `h1`, one button, the
UTM-tagged link and the platform's unsubscribe link, and must pass the template linter in its dialect.

Platform template tags (`{{ first_name|default:'there' }}`, `{% unsubscribe_url %}`, `%%unsub_center_url%%` and the
rest) come out of the compiler exactly as they went in; the renderer protects them during compilation and stops with
an error if one would be lost.

Things to know:

* **A logo is an image.** The text-to-image check fails an email with under 200 characters of readable text per
  image and warns under 250. A short email with a logo can trip it, especially on Klaviyo, HubSpot and Salesforce
  Marketing Cloud, whose address and unsubscribe tags do not count as text. A footer line or two (`extra_lines`)
  keeps short emails clear of it.
* **No dark-mode logo swap.** A second, hidden logo would count as another image and does not hide reliably in
  every client. Use a logo that reads on light and dark backgrounds.
* **Web fonts are a bonus.** Apple Mail, iOS Mail and Outlook for Mac load them; Gmail and Outlook for Windows show the stack.
* **The button in Outlook for Windows** keeps its color and padding, but only the label itself is clickable there.

## 6. Golden files

`templates/email/compiled/<dialect>/` holds one sample email per platform, compiled with the Ridgeview kit: the
`.mjml` source and the `.html` it compiles to. The tests recompile each source and require the same bytes, so an
upgrade of the compiler that changes the output is caught before it reaches anyone's inbox. After a deliberate
change to the components or the renderer, refresh them and review the diff:

```text
python3 engine/render/mjml_render.py --write-golden
```
