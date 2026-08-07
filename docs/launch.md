# Launch notes

Drafts and targeting for getting this in front of people. Not part of the
package; kept in-repo so it stays honest about what actually shipped.

**Before posting anywhere, check these are true:**

- [ ] `pip install parity-ci` works from a clean environment
- [ ] `parity demo` runs in under 15 seconds with no config and no key
- [ ] CI badge is green on `main`
- [ ] README's first screen renders correctly on GitHub mobile
- [ ] Every claim in the post is demonstrable in under a minute

The last one matters most. This audience checks.

---

## Positioning, in one sentence

> Traditional CI assumes the same input gives the same output. LLMs don't. Parity
> captures what your model actually did, replays it against a candidate, and
> tells you what broke.

Three framings, in descending order of how well they land:

1. **"`git diff` for model behaviour"** — instantly legible, no explanation needed
2. **"Your provider deprecates a model on Friday. What breaks?"** — the deadline is the hook
3. **"Snapshot testing for non-deterministic systems"** — precise, but assumes the reader knows jest

Avoid: "AI-powered", "revolutionary", "the future of", any number you can't source.

---

## Show HN

**Title** (80 char limit — states the thing, no adjectives):

```
Show HN: Parity – catch behaviour regressions when you swap LLM models
```

Alternatives if that reads flat:
- `Show HN: git diff, but for LLM behaviour`
- `Show HN: A CI gate for non-deterministic LLM output`

**URL:** `https://github.com/Medhovarsh/parity`

**First comment** (post immediately after submitting — HN expects the author to
explain themselves):

> I built this after watching a team spend a week migrating off a deprecated
> model with no way to answer "what will this break?" The review process was
> someone reading twenty outputs in a notebook and shipping on judgement.
>
> Traditional CI can't help because it assumes deterministic output. Eval tools
> can, but they all ask you to author test cases first — which is homework with
> no deadline, so it never gets done.
>
> Parity's bet is that you shouldn't author anything. It reads interaction logs
> you already have, and infers what to enforce from what the old model actually
> produced: if it returned JSON, the new one must too; if it called a tool, the
> new one must too; if it answered, the new one must not refuse. Those checks are
> deterministic and free, so most of a run costs nothing. A semantic judge is
> optional and only sees what the free checks can't settle.
>
> Two design decisions I'd push back on if I were reading this:
>
> **There's an `unverified` verdict.** When outputs differ and no judge is
> configured, it says "this changed and nobody looked" rather than guessing. It's
> not a pass and not a fail. Defaulting it to pass makes the gate a rubber stamp;
> defaulting it to fail blocks deploys over cosmetic rewording and gets the tool
> uninstalled. I'd rather report the truth and let you tighten it later.
>
> **`parity accept` exists.** Behaviour changes are often intentional. Without a
> way to approve one, a gate you can't satisfy is a gate people switch off — same
> reason jest has `-u`. Broken cases are refused unless you name them explicitly,
> so you can't bless a regression by accident just because it shared a run with
> an intended change.
>
> It runs with no API key at all: `parity demo` is a full run against a scripted
> offline model, and `--candidate ollama:llama3.1` works against a local model
> for free. The test suite makes zero network calls.
>
> Apache-2.0, Python 3.11–3.14, Linux/macOS/Windows on x86_64 and arm64. Happy to
> answer anything, and genuinely interested in cases where the inference-based
> checks would produce false positives on your workload — that's the failure mode
> I'm most worried about.

**Timing:** Tuesday–Thursday, 8–10am ET. Avoid Fridays and weekends.

**When comments arrive:** answer every technical question in the first two hours;
that's what decides whether it climbs. Never argue with criticism — if someone
finds a false positive, thank them and open an issue. A maintainer who takes a
bug well converts more readers than the original post.

---

## X / Twitter

Hook tweet — lead with the pain, not the tool:

> Your provider deprecates a model. You have prompts in production. You have to
> switch by Friday.
>
> Nobody can tell you what will break.
>
> Traditional CI assumes the same input → same output. Yours doesn't.
>
> So I built `git diff` for model behaviour 🧵

Then:

> 2/ It captures what your model *actually did* on real inputs — from logs you
> already have — replays them against the candidate, and classifies every
> difference.
>
> No test authoring. That's the whole bet.
>
> [screenshot of `parity demo` output]

> 3/ What it enforces is *inferred* from the baseline:
>
> baseline returned JSON → candidate must too
> baseline called search_orders → candidate must too
> baseline answered → candidate must not refuse
> baseline finished → candidate must not truncate
>
> None of that needs an LLM to detect. So it's free.

> 4/ When something breaks, it shows you *what* changed — structure first:
>
> ```
> - invoice.tax_total: 240.0
> ~ invoice.total: 1440.0 → 1200.0
> ```
>
> One line. Not a wall of red. A character diff of model output teaches you
> nothing because every line differs.

> 5/ Change was intentional? `parity accept` makes it the new baseline.
>
> Without that loop, a gate you can't satisfy is a gate people switch off.
> (Broken cases are refused unless you name them — no blessing regressions by
> accident.)

> 6/ Runs with zero API keys.
>
> `parity demo` → full run, offline, 10 seconds
> `--candidate ollama:llama3.1` → real models, local, free
>
> Test suite makes no network calls at all.

> 7/ Apache-2.0. Python 3.11–3.14. Linux/macOS/Windows, x86_64 + arm64.
>
> github.com/Medhovarsh/parity
>
> Most interested in hearing where the inferred checks would false-positive on
> your workload.

**Note:** attach the `parity demo` terminal output as an image on tweet 2. It's
the single most convincing artefact — it shows five real regression classes
being caught in one screen.

---

## Reddit

### r/LocalLLaMA

This community cares about local, free, and no-account. Lead there.

**Title:** `Built a regression gate for LLM output that runs entirely offline — no API key, works with Ollama`

> When you swap models — including swapping between local ones — nothing tells
> you what quietly broke. Output still looks fine, but a field vanished from your
> JSON, or the model stopped calling a tool and started answering from memory.
>
> Parity captures what your model actually produced on real inputs, replays them
> against a new model, and classifies every difference. It infers what to enforce
> from the old output, so you don't write test cases.
>
> Relevant to this sub specifically:
> - Zero API keys. `parity demo` runs offline with no config.
> - `--candidate ollama:llama3.1` works against your local models.
> - The optional semantic judge also runs on Ollama, so payloads never leave your
>   machine.
> - No telemetry, and that's enforced in CI rather than just promised.
>
> Apache-2.0. Genuinely want to hear where the inferred checks would be wrong for
> your setup.

### r/MachineLearning

Flair as `[P]`. This audience wants method, not pitch.

**Title:** `[P] Parity — structure-first regression detection for non-deterministic model output`

Lead with the classification design: deterministic checks first, semantic judge
only on the residual, and the explicit `unverified` verdict for "differs, nothing
compared them". That last decision is the interesting one to this crowd — it's an
honesty-vs-usability tradeoff, and they'll engage with it.

---

## LinkedIn

Different audience: this reaches VPs and buyers rather than builders. Slower, but
closer to where budget eventually sits. Write it about the *organisational*
problem.

> Every company shipping AI has the same unmanaged risk: nobody can prove what a
> model change does before it reaches customers.
>
> The current process is an engineer reading twenty outputs and making a
> judgement call. That's not a process — it's an individual absorbing
> organisational risk.
>
> I built Parity to make that reviewable. It captures what your model actually
> did on real production inputs, replays them against the candidate, and produces
> an artefact showing exactly what changed and who approved it.
>
> That artefact matters beyond engineering. EU AI Act enforcement, ISO 42001, and
> several US state laws all require evidence that a system behaves as claimed. A
> record of which behaviour changes were reviewed and accepted is that evidence.
>
> Open source, Apache-2.0, runs entirely on your infrastructure with no data
> leaving your environment.
>
> github.com/Medhovarsh/parity

Skip hashtag spam. Two at most: #AI #MLOps

---

## Who specifically

The original goal was reaching FAANG / AI-lab / YC / a16z-adjacent founders. In
practice you reach them by reaching the engineers who work for them.

| Audience | Where | What lands |
|---|---|---|
| AI infra engineers | HN, r/LocalLLaMA | offline, no keys, structure-first diff |
| Platform / DevEx teams | HN, LinkedIn | one CI step, JUnit output, exit codes |
| YC founders shipping LLM features | X, HN | zero setup, free tier is the whole tool |
| a16z / infra investors | X, LinkedIn | the category framing: gating *change*, not scoring *quality* |
| Compliance / risk leaders | LinkedIn | the audit artefact, EU AI Act angle |

**Direct outreach that isn't spam:** find teams who publicly posted about a
painful model migration — search X and HN for "gpt-4 deprecation", "model
migration", "prompt regression". Reply with something useful about *their*
specific problem, not a link drop. One good reply beats fifty cold DMs.

**Aggregators worth submitting to** once there's traction: Awesome-LLMOps,
Awesome-Production-ML, Python Weekly, TLDR AI, MLOps Community Slack.

---

## What will actually decide this

Not the copy. Three things:

1. **Does `parity demo` work on their machine, first try?** Any friction here and
   they close the tab. This is why the demo needs no config, no key, no network.
2. **Does the README's first screen make the problem obvious?** Ten seconds.
3. **Does the maintainer respond well in the first two hours?** More converting
   than the post itself.

The honest risk: the inferred checks could false-positive on workloads that
legitimately vary — creative generation, open-ended chat. If that comes up,
don't argue. It's a real limit: Parity gates *change* in systems that are meant
to be stable. Say so, and point at `parity accept` and per-case `skip_checks`.
