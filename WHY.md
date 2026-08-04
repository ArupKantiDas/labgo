# Why this exists

*A plain-English account of the problem, the idea, and what has actually been built.
No code. If you only read one file in this repository, read this one.*

---

## Tuesday

You are asked to change one function. It is called `parse_config`, it is nineteen lines
long, and the change is small. You make it, you run the tests, they pass. You ship it.

On Friday, billing breaks.

The chain took a while to reconstruct. A reporting module called a helper, which called a
validator, which called `parse_config` and depended on it raising an exception rather than
returning `None`. Four steps away, in a directory you had never opened. Nobody flagged it,
because nobody knew. The engineer who would have known left in March.

This is not a story about carelessness. You did the normal things and they were not enough.
It is a story about a question that every developer asks constantly and nobody can answer
reliably:

> **If I change this, what else is affected?**

## Why the question is hard

The obvious approaches all fail in the same way.

**Searching** for `parse_config` shows you who calls it directly. It does not show you who
calls *them*. The damage is almost never one step away — if it were, you would have seen
it. It is three or four steps out, and each step multiplies what you would have to check
by hand.

**Documentation** describes the system as it was understood at the moment somebody wrote
it down, which is not the same as how it behaves now.

**Asking a colleague** works right up until the person who knows has moved teams, or
forgotten, or only ever knew their own corner of it.

**Asking an AI chatbot** feels like it should work, and mostly does not. It reads a handful
of files, notices some things that look related, and writes a confident paragraph. It has
no way to actually follow the chain, so it produces something that reads like an answer and
isn't one. That failure mode is worse than no answer, because it is persuasive.

So people ship changes half-blind, and find out on Friday.

## The idea

Build a map of the codebase. Not a list of files, but a map of *relationships*: which
function calls which, which file imports which, which test exercises which code, which
files have historically been changed together.

Then the question stops being a research project and becomes navigation. You start at the
thing you are about to touch, and you follow the connections outward.

## The distinction that makes this work

There are two genuinely different ways to find things, and most people building AI systems
today only reach for one of them.

The first is **semantic search** — the thing behind "chat with your documents" and most
retrieval tools. It finds material that *means* something similar to what you asked. Ask
"how do we handle retries" and it surfaces the retry documentation even if you never used
that word. Think of an excellent librarian who understood what you meant rather than what
you said.

The second is a **graph**, which stores connections and lets you walk them. Think of a
subway map. It is useless for "recommend me a nice station." It is unbeatable for "this
station is closed on Sunday, which journeys are disrupted?" — because that is a question
about following lines outward from a point.

Here is the whole idea in one sentence:

> **"What breaks if I change `parse_config`?" is a subway question, not a librarian
> question.**

Ask a semantic search engine and it will return other parsing functions. Code that
*resembles* `parse_config`. Which is precisely, almost comically, the wrong answer. You did
not want things that look alike. You wanted the chain of things that depend on it.

Following a chain of connections is not something semantic search does badly. It is
something semantic search cannot do at all, in the same way a dictionary cannot tell you
the fastest route across a city. Different tool, different question.

That is why this project is built on a graph database, and why the graph is not decoration.
Take it out and the central question becomes unanswerable.

Semantic search still earns its place, though, because some questions really are librarian
questions. "Where do we document our retry policy?" has nothing to do with call chains. A
system that only has a graph will answer that one badly. So the system needs both, and the
real skill — the thing worth learning here — is knowing which kind of question you have
been handed.

## How we will know whether any of it works

This is the part I would most want you to take away, because it is what separates a project
that survives scrutiny from a demo.

Normally, evaluating a system like this is miserable. You would need experienced engineers
to sit down and hand-write correct answers for hundreds of scenarios. It is slow, it is
expensive, and two of them will disagree with each other about half the time.

But there is a shortcut hiding in plain sight, and it is the best idea in this project.

**Git history is already full of correctly-answered versions of exactly our question.**

Every commit ever made is a developer saying: *when I changed this file, I also had to
change these other files*. That is a question and a verified answer, recorded at the moment
somebody actually did the work. A mature repository contains thousands of them, sitting
there, free.

So we set an exam. Take a real commit from two years ago. Hide everything except the first
file that changed. Ask the system what else needs to change. Compare its answer against what
the developer actually did.

Do that six hundred times and you have a score that means something. Nobody had to label
anything.

It also lets us test the two halves separately, which almost nobody bothers to do:

- Did the system **find** the right files?
- Given the right files, did it **reason** about them correctly?

When the answer is wrong, you immediately know which half failed. Without that separation
all you learn is "it's wrong somewhere," which is the same as learning nothing.

## Why more than one agent

A single AI asked to trace call chains, read tests, check history, and write a summary all
at once does each of those things worse than one asked to do only the first. Attention is
finite, for models as much as for people.

So the work gets split the way a team would split it. Something decides what needs
investigating. Several things investigate different angles at the same time. Something
combines the findings. Something checks the result before a human ever sees it.

The part that surprises people: **the hard problems here are not AI problems.** They are
coordination problems. Two agents editing the same file and quietly destroying each other's
work. Three findings that contradict and no principled way to reconcile them. One agent
being fluently, confidently wrong, and the others believing it because it sounded certain.

That is where the real engineering is, and it is the reason this project cares so much about
isolation and verification.

## What exists today

Two programs. Neither of them uses AI, and that is deliberate.

**The map-maker** reads every Python file in a codebase and works out who calls whom. Run
against the `httpx` library, it found 1,301 things — files, functions, classes — and 2,100
connections between them.

**The answer key** reads git history and builds the exam. From httpx's 1,482 commits it
produced 610 questions with verified answers, along with a record of which files have a
habit of changing together.

So: the map and the answer key. The thing that actually answers questions does not exist
yet. That ordering is on purpose, because you cannot tell whether an answer-machine is any
good until you already hold the answers.

## Two numbers, honestly

**The map-maker resolves 27% of function calls.** When it sees `thing.save()`, it tries to
work out which `save` that points to, and about a quarter of the time it can. The reason it
is not higher is worth understanding: Python decides what `thing` is while the program is
running. Reading the source, `thing` could be a User, an Invoice, or something assembled
from a config file at startup. This is not sloppiness on our part. For a language like
Python it is impossible in principle without running the program.

Tools exist that would push the number up by inferring types, at a real cost in complexity
and speed. The decision recorded in the log was to not buy that complexity until the exam
proves this is what is holding us back — because it might not be. The "these files always
change together" signal may turn out to carry most of the weight on its own.

That restraint is most of what architecture is.

**610 exam questions.** That is the measuring stick for everything from here.

## What happens next, and why it matters more than it sounds

The next thing to build is the stupid version. No AI anywhere near it. Just: *show me files
that have historically changed alongside this one, plus anything within two steps on the
call map.*

Then score it against the 610 questions. Suppose it gets 45%.

That unremarkable number is then the most valuable thing in the project, because when the
agents arrive and score 70%, you can say exactly what they were worth. And if they score
47%, you have learned something more useful still: that the expensive, slow, complicated
layer bought you two points, and the problem was somewhere else all along.

Skip the stupid version and you end up with a system that scores 70% and no way to know
whether that is excellent or whether the question was easy. Nearly every AI portfolio
project skips it. It is the reason so many of them fall apart under one question:

> *How do you know the agents helped?*

The whole point of building it in this order is to be able to answer that.
