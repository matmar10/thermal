---
name: folder-labels
description: Print folder labels in two sizes, each with its own calculation and default dimensions. "manila" is a stick-on tab label on the adhesive roll, size 3 down to 2 text, upright or sideways, padded to the label's length. "hanging" is a paper insert for a hanging folder's plastic tab, on a non-adhesive roll, size 2 only, kept to the insert's width even on the full 80mm roll. Either can be sized to the user's own label dimensions. Also handles a whole filing system in batches, one roll at a time, resumable after a stock-out. TRIGGER when the user asks to label or print labels for folders, file folders, manila/manilla folders, hanging folders, hanging files, suspension files, Pendaflex tabs, tab inserts, folder tabs or a filing system, or gives a label size (e.g. "50 x 20 mm", "2 by 3/4 inch") for tab text.
allowed-tools: Bash(print-to-thermal *)
---

# Folder labels

This skill builds folder-label jobs from the `thermal` skill's primitives. Use
the `thermal` skill for printer setup, the job format, `preview` and `print`.

## The two sizes

Both were tested on real folders. The width limit was 384 dots (48mm at 8
dots/mm) in both cases.

| | **manila** | **hanging** |
|---|---|---|
| What it is | Label stuck on a manila folder's tab | Paper insert slid into a hanging folder's plastic tab |
| Roll | **Adhesive** (`label-60-adhesive`) | **Non-adhesive**: `label-60-plain` or the full-width `receipt-80` |
| Default face, W × H | 48 × 22mm | 48 × 16mm |
| Text | Bold, centred, size 3 stepping down to 2 | Bold, centred, size 2 only |
| Lines | At most 2 | At most 2 |
| Calculation | A below | B below |

The user may write "manilla". Treat it as manila. W is the label's width
along the folder edge, the direction the text reads. H is its height. If the
user gives either, use their numbers instead of the defaults (inches × 25.4 =
mm). If their stock ids differ, match them in `print-to-thermal stocks` by the
`adhesive` flag and width.

These rules apply to both sizes:
- Size 1 can't be read on a tab.
- Never split a word across lines: the printer would break it mid-word
  (`EDUCATIO`/`N`).
- Never spend the adhesive roll on hanging inserts.
- Manila and hanging labels are never printed in the same run. If both are
  needed, print two batches and stop between them so the user can swap rolls.

Read these numbers from `print-to-thermal stocks`: the stock's `print_dots`
and `width_mm`, and the printer's `dots_per_mm`, `cell_dots`,
`max_text_size`, `line_dots` and `dead_zone_mm`. Below,
`across = print_dots / dots_per_mm`, which is 48mm on the 60mm rolls.

At size *s*, a line of text has `cols(s) = floor(min(W, across) × dots_per_mm
/ (cell_dots × s))` characters and is about **3.3 × s mm** tall. At W = 48mm
that is 16 characters and 6.75mm at size 2, and 10 characters and 10mm at
size 3.

## A. Manila: stepped size, upright or sideways, padded to length

1. **Orientation.**
   - **Upright** (the default) when W ≤ the roll's `width_mm`. Text runs
     across the roll, and H is the length of the cut piece.
   - **Sideways** when W is wider than the roll and H ≤ `across` − 4mm. The
     text is a one-line `big` banner running along the strip, so W becomes
     the piece length. This suits a long, low tab, such as a full 3½-inch
     manila tab.
   - **Neither** when the label won't fit either way. Offer two pieces that
     butt together, or a smaller label.

2. **Upright size.**
   - **Line limit:** `min(2, floor((H − 2) / (3.3 × s)))`.
   - **Ceiling:** start at size 3. If the user's H has room, start at
     `max_text_size` instead.
   - **Step down to size 2.** Take the first size where every word fits in
     `cols(s)` and the wrapped text fits within the line limit.
   - **If nothing fits at size 2,** don't go smaller. Allow one more line, or
     suggest a shorter form (`&` for "and"), and say which you chose.
   - **Spare height, short of width:** `"size": [w, h]` with h one step above
     w (e.g. `[2, 3]`) fills the height better.
   - **Larger than size 4:** use one `big` block per line, with height
     `c ≈ (H − 2) / lines / 1.4` mm and `{ "type": "space", "mm": 1.5 }`
     between lines. If preview warns that `big` "shrunk to fit the width",
     lower `c` or wrap the text differently.

3. **Sideways size.**
   - Use `{ "type": "big", "text": "...", "height": c, "sideways": true }`.
   - Set the cap height from H: `c ≈ (H − 2) × 0.75` for ALL CAPS, or × 0.6
     when the text has descenders (g, j, p, q, y).
   - The banner's length grows in proportion to `c`. Preview it, then
     measure `banner = labels_mm − feed_mm`.
   - If the banner is longer than W − 4, set `c = c × (W − 4) / banner` and
     preview again. For example, "Grades & Enrollment" at c = 5 is 77mm long.

4. **Pad to length L** (H upright, W sideways). The piece is
   `dead_zone_mm + labels_mm` long, where `labels_mm` comes from
   `preview --json` and includes the feed before the cut. That feed is
   `feed_mm = 3 × line_dots / dots_per_mm`, which is 11.25mm here.
   - Take `content = labels_mm − feed_mm`.
   - Add a `space` of `(L − content) / 2 − dead_zone_mm` mm before the text.
   - Add a `space` of `(L − content) / 2 − feed_mm` mm after it.
   - If either comes out negative, leave the padding out. About 19mm of every
     piece is unavoidable blank paper, so tell the user how much to trim.
   - Never lower `feed`: a shorter feed cuts into the last line.

## B. Hanging: fixed size, sized to the insert, trimmed by hand

1. **Size is always 2.** Size 3 is too tall for the tab window, and size 1
   can't be read through the plastic.
2. **Width.** Wrap at `cols(2)` from `min(W, 48mm)`: 16 characters by default.
   The insert sets the width, not the roll. On the full-width 80mm roll, still
   wrap at 16, not the roll's 24. The text prints centred and the user trims
   the sides to fit.
3. **Lines.** The limit is `min(2, floor((H − 2) / 6.75))`. If a word is
   longer than `cols(2)`, or the text needs more lines, don't shrink it.
   Suggest a shorter form, or accept a third line, and say so.
4. **Length.** Don't pad. Every piece is at least about 26mm long, mostly
   blank paper the printer always leaves. The user trims it to H.

## Writing the job

Wrap the text yourself and put it in one `text` block, joined with `\n`, with
`"wrap": false`. That keeps the printer from re-wrapping it to the roll's own
width:

```json
{ "type": "text", "text": "Grades &\nEnrollment", "size": 3, "bold": true, "align": "center", "wrap": false }
```

For a batch, put every label in one job with a `cut` block between them, and
name the `stock`. Preview it, fix any `warning:`, show the user the picture
and say which size each label landed on.

## Filing-system conventions (the user's own system)

- **Hanging folders:** one per category, labelled in CAPITALS (`AUTOMOTIVE`,
  `TAXES`).
- **Manila folders:** one per topic, inside the category's hanging folder,
  labelled in Title Case.
- **Per-person topics:** when a topic has one manila folder per family member,
  the manila labels read `Name - Topic`. The topic also gets a hanging folder
  of its own, with two inserts: the CATEGORY and the topic.
- **Order:** sort by category, then topic, so the numbering is the same on
  every run.

## Printing

1. **Check the roll.** Ask which roll is loaded and record it with
   `print-to-thermal stock <id>`. A job naming a different stock is refused,
   which protects the adhesive roll.
2. **Print one label** and have the user hold it against a tab, or slide it
   into one, before sending the rest.
3. **Print in chunks of about 10.** After each chunk, say which numbers were
   sent, so a stock-out or misfeed resumes from the next number instead of
   reprinting everything. "Sent" means the printer accepted the job, not that
   paper came out.

**No measurements?** Print the same text at sizes 4, 3 and 2 as one job, with
a `cut` between them. Use the largest one that fits the user's tab as the
ceiling. For a manila label that wraps over the tab edge, keep the text within
one face, H/2.
