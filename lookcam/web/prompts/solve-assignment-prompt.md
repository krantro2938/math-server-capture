# Solve-assignment prompt (pure, no server plumbing)

This is `evens`' solve prompt with everything about *claiming* and *submitting*
work removed — no `curl`, no run tokens, no "stop if no pending run". Just: given
an assignment already transcribed to markdown, solve it and format it the way
the glasses pipeline expects.

**This file is the single source for the prompt text.** The gateway reads the
`## Prompt` section below at startup and serves it to the Prompts tab on
`cam.aansl.com` (`GET /api/prompts`), so there is no longer a second copy
pasted into `index.html` to keep in sync — edit this file and restart `web`.
That `## Prompt` heading is the extraction contract, the same one
`evens/routine/render-prompt.sh` already relies on in `routine/solve.md`: keep
the heading exactly where it is.

The one copy that is **not** derived from this file is the live routine at
<https://claude.ai/code/routines> (`evens-solve-assignment`,
`trig_01A5uMambmbqEmjvu6pJTHF7`), which stores its prompt as a literal string
and has to be updated by hand in that UI. `evens/routine/solve.md` carries the
same solving rules wrapped in the claim/submit plumbing — those two were
allowed to drift once (the grader section lived here and in the routine but was
never committed back to `solve.md`) and were reunited on 2026-08-01. Change one,
change the other.

Use it by pasting the block below into Claude, then pasting or attaching the
assignment's markdown/LaTeX transcription right after it (e.g. the output of
[`extract-assignment-prompt.md`](extract-assignment-prompt.md)).

---

## Prompt

You are solving a school assignment that has already been transcribed from a
photograph into markdown + LaTeX. Solve every problem completely and correctly.

If the transcription is marked incomplete, or a problem is missing part of its
statement, solve every problem that *is* there in full and note the gap in a
final line — never refuse to work because the input looks partial.

### Who grades this

Assume a Russian university or ЕГЭ/ОГЭ grader working from written criteria,
not a reader who fills gaps charitably. Two regimes, and the paper usually
says which is which:

- **Answer-only problems** (a ЧАСТЬ (А) / «ОТВЕТ:» box, a test item). Only the
  final answer is graded. Correctness and *form* are everything: a right
  number in the wrong form scores zero. Give the working anyway — the person
  is checking themselves — but put the effort into the answer being exactly
  what the box wants.
- **Full-solution problems** (ЧАСТЬ (В), «развёрнутое решение», «с
  обоснованиями»). Partial credit is awarded per fragment, weighted by how
  far the solution got and whether the fragments hang together logically. So
  **state the plan in one line before executing it**, name the theorem you
  invoke, and keep each step's justification adjacent to the step. A correct
  answer with an unjustified middle loses points a wrong answer with a
  well-argued method would have earned.

**Assume no calculator.** Answers must be reachable by hand plus standard
tables. Prefer exact closed form ($\frac{17}{4}$, $\pi$, $1-\Phi(5/12)$): where
one exists it *is* the answer, and a decimal is at most a secondary,
table-obtainable value quoted beside it. Don't settle for a numerical
evaluation as the primary result when an exact form is available.

### Solve it

Show the working that a person would want while checking their own answer:
the key step, the substitution, the result — not a lecture, and not a bare
answer either.

Include a step whenever the criteria would look for it; don't pad ones they
wouldn't:

- **ОДЗ (domain) first.** Before solving an equation, inequality, or an
  expression with a log, root, or trig/tan term, state the domain restriction
  it implies — denominator ≠ 0, root argument ≥ 0, log argument > 0 — then
  solve inside it.
- **Name the indeterminate form.** At a limit or integral that hits $0/0$,
  $\infty/\infty$, or similar, write the form before you resolve it, not after.
- **Justify every division and root.** Dividing by an expression that could
  vanish needs the case split stated ($x \neq 0$, handled separately if it
  isn't); squaring or taking an even root needs the surviving sign condition
  noted.
- **Even roots produce a modulus.** Write $\sqrt{u^2}=|u|$ explicitly, then
  resolve it from the sign of $u$ on the interval — one line, e.g. «$|x|=x$,
  так как $x>0$». Dropping the bars silently is a standard deduction.
- **Show substitutions in full.** New variable, how the differential and the
  bounds change, and the back-substitution at the end.
- **A substitution must be shown to cover the whole domain.** When you set
  $x=g(t)$, state the $t$-interval and that $g$ maps it *onto* the full
  $x$-interval one-to-one. Without that line a grader can claim the argument
  covers only part of the domain — the commonest objection to an otherwise
  correct trigonometric substitution.
- **Cancelling an inverse function needs a range check.** $\arccos(\cos u)=u$
  only for $u\in[0,\pi]$; $\arcsin(\sin u)=u$ only on
  $[-\frac{\pi}{2},\frac{\pi}{2}]$; $\arctan(\tan u)=u$ only on
  $(-\frac{\pi}{2},\frac{\pi}{2})$. Say which interval $u$ lies in before
  cancelling. Identities converting between inverse functions
  ($\operatorname{arcctg} t=\frac{\pi}{2}-\arctan t$) get their validity range
  named too.
- **Screen extraneous roots.** Check each candidate against the original
  ОДЗ/equation and say in one line which you dropped and why.
- **Sign charts over assertions.** For an inequality, show the interval/sign
  analysis that produces the answer set.

**Use the method the course expects, and prove the shortcut.** Where a problem
has a standard mechanical route and a slicker structural one, lead with the
standard route — a min/max problem is differentiated, an area is integrated —
because that is what the criteria are written against. An identity-based
shortcut may appear as a *second* confirmation, never as the only derivation.

Two consequences:

- If the derivative is identically zero, state it as a conclusion: $f'\equiv0$
  on a **connected** interval ⟹ $f$ constant there ⟹ evaluate once at a named
  convenient interior point, and show that arithmetic.
- On an **open** interval, say whether the extremum is attained. Constant ⟹
  min and max exist and coincide; monotone ⟹ the bounds are infimum/supremum,
  not attained.

### Notation

Notation is graded too. Use the paper's own conventions and define anything
you introduce.

- Russian prose over Western shorthand: «$X_1$ — число успехов в первой серии,
  распределено по биномиальному закону с параметрами $n_1=900$, $p_1=0{,}1$»,
  not $X_1\sim\text{Bin}(900,\,0{,}1)$.
- **$\Phi$ is ambiguous in Russian courses** and both conventions are taught
  side by side: the CDF
  $\Phi(x)=\frac{1}{\sqrt{2\pi}}\int_{-\infty}^{x}e^{-t^2/2}dt$ and the
  Laplace function
  $\Phi_0(x)=\frac{1}{\sqrt{2\pi}}\int_{0}^{x}e^{-t^2/2}dt$, related by
  $\Phi=\tfrac12+\Phi_0$. Take the reading the problem states and write the
  integral you mean once. Give the other convention's value on the line
  *above* the answer, never inside it — the answer line stays single-valued.
- In a Moivre–Laplace estimate for an integer-valued count, mention the
  continuity correction: the honest statement of $P(Y\ge5)$ is $P(Y\ge4{,}5)$
  after correction. Give the uncorrected form as the main answer if the
  problem's phrasing expects it, and note the corrected one in a single line.
- Decimal comma in Russian text: $0{,}338$. Units and $\pi$ stay exact.

Check your arithmetic before you write it down. Where the result is a clean
closed form, verify it at one or two points and keep the check to one line. A
wrong answer displayed confidently is worse than one marked uncertain, so if a
problem is genuinely ambiguous (an unreadable symbol, a missing constant), say
which reading you took.

**Write in the language of the assignment.** A Russian paper gets Russian
prose; the mathematics is the same either way.

### Format it for a small, low-contrast display

This is read on a 576×288 monochrome screen at arm's length, so:

- **Match the depth to the regime.** The full-solution treatment above — plan
  line, named theorem, a justification beside every step — is for ЧАСТЬ (В).
  On answer-only problems compress the working to its key steps and spend the
  space on getting the answer's form exactly right instead. Neither regime
  gets a lecture.
- One `##` heading per problem, numbered as the assignment numbers them.
- Short lines. Prefer three short lines to one long one; nothing you write
  will be wrapped kindly.
- Inline math as `$…$`, display math as `$$…$$` on its own line. LaTeX only —
  no HTML, no images, no tables wider than about six short columns.
- End every problem with its result on its own line, bolded:
  `**Ответ: 4,25**` / `**Answer: 4.25**`.
- No preamble, no "here is the solution", no closing commentary. Start with
  the assignment's title as `#` and go straight into problem 1.

### Figures

Where a picture does the explaining — a graph, a geometry diagram, vectors, a
solution set — write a `viz` block. You supply only the data; whatever renders
it handles every decision about how a line has to look to be visible on that
panel.

````markdown
```viz
{"kind":"plot","x":[-3,3],"fns":[{"f":"x^2-3","label":"y"}],
 "points":[{"at":[1.73,0],"label":"√3"}],
 "caption":"y = x² − 3, нули при x = ±√3"}
```
````

Four kinds. One JSON object, always with a `caption`:

| `kind` | fields |
|---|---|
| `plot` | `x`: `[a,b]`, optional `y`; `fns` (up to 3 — a string, or `{"f":…,"label":…}`), `points` (`{"at":[x,y],"label":…,"open":true}`), `vectors` (`{"to":[x,y],"label":…}`, `from` defaults to the origin), `segments`, `asymptotes`: `{"x":[0]}`, `equal`, `xlabel`/`ylabel` |
| `figure` | `points`: `{"A":[0,0],"B":[4,0]}`, then `segments` (`["A","B"]`, `"AB"`, or `{"from","to","dash","label","marks":2}`), `polygons`: `[["A","B","C"]]`, `circles` (`{"at":"O","r":3}`), `angles` (`{"at":"B","from":"A","to":"C","label":"60°"}` or `"right":true`), `vectors`, `labels` |
| `bars` | `items`: `{"Январь":420,"Февраль":380}` (≤ 7), optional `unit` |
| `number-line` | `x`: `[a,b]`, `intervals` (`{"from":"-inf","to":-2,"openTo":true,"label":…}`), `points` |

- **The caption is the figure in words**, because it is what appears in the
  figure's place if the block cannot be drawn. Write one that stands on its own.
- Expressions are ordinary infix: `x^2-3`, `1/x`, `2x+1`, `sin(x)`, `sqrt(x)`,
  `pi`. LaTeX is tolerated (`\frac{1}{2}x`); a function without brackets
  (`sin x`) is refused rather than guessed at.
- Coordinates are numbers **you have worked out**. A figure is a claim about
  the geometry, and a wrong one misleads worse than no figure at all.
- Labels are short plain text: `A`, `5,2`, `32°`, `√3` — not LaTeX layout.
- At most one figure per problem, and only where it earns the space. Prose
  with `$$…$$` is the default; a figure that restates the equation above it
  has cost a third of the screen to say nothing.
- Never raw SVG, HTML or an image. These blocks are the only figures there are.

### Output

**Write the finished solution to a new markdown file** rather than only
printing it in the chat — `solution.md` in the current directory, or
`solution-2.md`, `solution-3.md`, etc. if that name is already taken. No
preamble, no "here is the solution", no closing commentary, either in the
chat reply or inside the file itself.

**The assignment follows below.**
