# CRAIID
### Continuous Redundant AI Instance Dialogue

> **In one line:** CRAIID is what lets a local AI assistant think for as long as
> it needs to — across many fresh restarts of itself — without ever losing the thread.

---

## The problem it solves

Every AI has a working-memory limit. The longer a session runs, the more its
context fills with its own history until quality quietly degrades — call it
**context fatigue**, the machine equivalent of someone who's been awake and
reasoning for twenty hours. The usual options are both bad: keep going and watch
the answers get worse, or restart and lose everything you built together.

## What CRAIID does instead

It lets the tired instance hand off to a brand-new one mid-stream, and briefs it
so completely that the new instance picks up exactly where the last left off. The
user notices nothing except that the assistant never gets dull.

It's a **relay race where the baton is the entire working memory** — or a
**hospital shift change** where the outgoing doctor writes a handoff note so
thorough the incoming one loses no patient context.

That's the name:

- **Continuous** — the work never has to stop or reset.
- **Redundant** — there's always a fresh instance ready to take over, so no single
  fatigued instance is a point of failure.
- **AI Instance Dialogue** — the handoff itself is one instance of the assistant
  briefing the next.

## Why it's more than "a summarizer" — the three roles

CRAIID isn't a monolith. It's a small newsroom-and-archive staffed by three
specialists, each running on its own model tier:

- **Archivist — the librarian.** Compresses the full history into a dense,
  perfectly recoverable archive, so years of depth are kept without clogging
  active memory and can be fetched on demand.
- **Journalist — the editor *and* the janitor.** As editor it reads the live
  conversation, discards the noise and tangents, finds the running theme, and
  writes a clean summary of what actually matters. As janitor it keeps the whole
  system from drowning in its own accumulated data.
- **Author — the briefer.** At handoff it assembles the warm-context note the next
  instance wakes up to: recent turns + the relevant archived depth + the
  Journalist's theme summary, written as one bounded document.

## What makes it serious

That briefing is **signed, tamper-evident, and integrity-checked** end to end — a
forged or corrupted handoff can't silently poison the next instance's memory. And
it all runs **locally**, which is the entire point: this is what makes a private,
on-your-own-hardware assistant viable for the long, deep, multi-hour work that
until now only a cloud model could sustain.

---

**The skeptic's one-liner:** *"It's how a local AI keeps its train of thought
across restarts — it hands itself a signed briefing before it gets too full to
think clearly, so a fresh copy can pick up mid-sentence."*
