# Technical screen: deployments an operator can trust

Target time: 2 hours. Please stop at 2.5 hours. You will not have time to fix
everything you find; choosing what not to fix, and saying why, is part of the
work and scores as well as a repair.

## The customer story

A marketer approved one landing page and three emails in CharacterQuilt and
asked for them to be pushed into HubSpot as drafts.

Deploying is the risky part. The process can be interrupted partway through,
and operators say that what CharacterQuilt reports afterwards does not always
match what they find in HubSpot. The starter's crash-and-restart demo runs
clean anyway. The operator's report and events recorded from real runs are in `fixtures/`.
The cases in the event log are labelled `c1`…`c11` and nothing more; grouping
them, and deciding which are the same fault and which are noise, is your job.
At least one is unrelated to anything the operator reported. Some of what is
wrong here does not show up by reading the code.

This repository stands in for that system using SQLite and a local fake HubSpot
client. It makes no network calls and needs no credentials, so you can crash
and restart it as often as you like.

## Your assignment

Decide what this deployment service should promise an operator, write that
promise down, and make the smallest set of changes that lets you back it up.

We expect you to work with a coding agent — Claude Code, Codex, or equivalent —
and to record the whole session. We are evaluating how you use the agent, not
just the repository it leaves behind. Before you or the agent edit any source
or test file, use the evidence to form your own view of the problem, direct the
agent as you develop `ROADMAP.md`, and commit that roadmap on its own. During
the rest of the work, challenge assumptions and inspect the evidence yourself.
A one-line request for an agent to complete the exercise unattended is not a
passing submission, even if the resulting code looks good. Nothing here tells
you what belongs in the roadmap; deciding that is part of the exercise, and a
roadmap worth reading contains claims another engineer could disagree with.

Removing a symptom is not the same as removing its cause. Both can be the right
call under time pressure; shipping one while describing it as the other is not.

By the end you should be able to explain:

- what the service guarantees, in plain language;
- when it is entitled to tell an operator that a deployment succeeded;
- for each guarantee, the check that fails first if it stops holding;
- which failures you handle, and how the behavior differs across them;
- which of your changes removed a cause, and which only stopped a symptom from
  appearing;
- what this design still cannot promise, even after your changes.

Add whatever tests and demonstrations you need to support those claims. You can
change the implementation and the tests freely. Keep both make commands
working.

## Constraints

- The required path stays local and easy to follow.
- `make stress` runs the deployment queue the way the worker pool runs it.
  Read what it reports; it is not there for decoration.
- `fixtures/` holds more than one approved request. They are different shapes
  on purpose, and a rule that looks right against one of them is not
  automatically right against the others.
- Assume more than one worker process can be running against the same database
  and the same provider at the same time. Operators restart things.
- Use the SQLite storage and the fake HubSpot client the starter provides;
  don't swap in something else. The provider is not yours to change — treat it
  the way you would treat HubSpot. If you need to simulate how it behaves, do
  that around it, not inside it.
- No queue, workflow framework, UI, container, cloud service, real model, OAuth
  flow, or real HubSpot integration.
- No special-casing of values that happen to appear in the fixtures.
- Don't repair behavior you can't tie to the operator's report.
- A narrow promise you can prove beats a broad one you can't.

## What to send back

- the repository, with its Git history;
- the complete raw transcript of your agent session, including the parts that
  went nowhere — please don't tidy it into a cleaner story;
- your `ROADMAP.md`, committed before any source or test edit;
- at least one thing in your write-up that you found yourself rather than took
  from the agent — a probe you ran, a state file you opened, a failure you
  reproduced — with that step visible in the transcript;
- your code and whatever checks you added;
- `make demo`, `make test` and `make stress` working, with their output;
- `DECISIONS.md` and `SUBMISSION.md` filled in, including the time you actually
  spent.

`make demo`, `make test` and `make stress` already run on the starter, so a green run is not
evidence that you are done. There are no hidden tests and no automatic grade. A
person reads the roadmap, the transcript, the code, and your explanation.
