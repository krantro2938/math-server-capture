# Extract-assignment prompt (image(s) → markdown)

`evens`' actual reader (`lookcam/assignment/server.ts`) drives Gemini in a
loop: one frame at a time, structured JSON output, persisted state across
captures, camera-nudging advice for a ceiling-mounted rig. None of that
machinery belongs in a prompt meant to be pasted into Claude with one or a
handful of photos already in hand — so this keeps the transcription rules
that matter (evidence-only, LaTeX-in-prose, don't invent, don't duplicate
across photos) and drops the JSON schema, the camera-framing fields, and the
turn-by-turn state.

Use it by pasting the block below into Claude and attaching one or more
photos of the assignment (different angles/close-ups of the same sheet are
fine — say so if some photos are close-ups of a part already covered by a
wider shot). The output is markdown ready to hand to
[`solve-assignment-prompt.md`](solve-assignment-prompt.md).

---

## Prompt

You are transcribing a school assignment from one or more photos of a sheet of
paper. The photos may be the whole page, or several close-ups covering
different parts of the same page — treat them as one document, not separate
assignments.

**EVIDENCE-ONLY RULE: never guess, complete, or reconstruct text that is not
actually legible.** A blurry character, a cropped line, a glare-covered word,
or a too-small problem is not readable. If any part of a problem is uncertain,
say exactly what's missing rather than inventing it — "problem 4's final line
is not visible" is correct; a plausible-looking guess at what that line says
is not. It's fine, and expected, for a problem to be marked incomplete.

**Math must be LaTeX, prose must not be.** Every mathematical symbol,
fraction, exponent, root, integral, matrix, or equation goes in LaTeX: inline
as `$...$`, display as `$$...$$`. Never approximate math with plain text
(`$\frac{3}{4}$`, not `3/4`; `$x^2$`, not `x2`).

A problem statement is **ordinary sentences with `$...$` islands in them** — it
is not one long LaTeX expression, and never wrap plain words in `\text{}`. If
you catch yourself writing `\text{}`, the words belong outside the math, not
inside it.

```
RIGHT: Отрезки $AB$ и $CD$ являются хордами. Найдите $AB$, если $CD = 18$.
WRONG: \text{Отрезки } AB \text{ и } CD \text{ являются хордами.}
```

**Combine across photos, don't duplicate.** Match problems by their number.
If a later photo shows a clearer or more complete version of a problem you've
already transcribed, replace it rather than adding a second copy. If a photo
is a close-up covering only part of a problem, use it to fill in or correct
that problem, not to create a new one.

**Only include a problem when every character of its statement is legible**
across the photos you were given. If part of it is unreadable or wasn't
captured in any photo, still list the problem (so nothing is silently
dropped), mark it incomplete, and say precisely what's missing.

Output the transcription as markdown, in this shape:

```
# <assignment title, if there is one>

*<subject, if identifiable>*

<any general instructions, in prose with $…$ islands, if present>

## <problem number>

<statement, in prose with $…$ islands>

## <next problem number>  _(incomplete)_

<whatever of the statement is legible>
```

- Number problems exactly as the assignment numbers them (not renumbered).
- Omit the title/subject/instructions lines entirely if the sheet doesn't have
  them — don't invent placeholders.
- Append `_(incomplete)_` to a problem's heading only when something about it
  is missing or illegible; otherwise leave the heading bare.
- After the problems, add one short line listing anything you couldn't read
  at all across every photo (e.g. "Problem 6 not visible in any photo") — omit
  this line if there's nothing to report.

**Write the result to a new markdown file** rather than only printing it in
the chat — `assignment.md` in the current directory, or `assignment-2.md`,
`assignment-3.md`, etc. if that name is already taken. No preamble, no "here
is the transcription", no closing commentary, either in the chat reply or
inside the file itself.

**The photo(s) follow below.**
