# CharacterQuilt technical screen: deploy a campaign safely

The assignment is in [TASK.md](TASK.md). What I did with it is below.

## The promise

> A deployment is one approved request, identified by the operator's
> idempotency key. Every approved asset becomes at most one draft in the
> destination, however many times the process crashes, restarts or is retried.
> The service calls a deployment `succeeded` only after reading every one of
> those drafts back out of the destination and finding it matching what was
> approved. Anything else gets a status that says what is wrong.

## Where to look

| file                             | what it is                                                                                                                                             |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [ROADMAP.md](ROADMAP.md)         | The plan, committed before any source edit. Evidence, the grouping of `c1`–`c11`, the eight guarantees, and five claims another engineer could reject. |
| [docs/DESIGN.md](docs/DESIGN.md) | The visual walkthrough — how it was designed and how each fault was fixed. Start here if you want the argument in pictures.                            |
| [DECISIONS.md](DECISIONS.md)     | What changed against the roadmap, what the agent did and where I overrode it, cause vs symptom, what is still unsafe.                                  |
| [SUBMISSION.md](SUBMISSION.md)   | Command output, the claim I am least sure of, and what a reviewer should know first.                                                                   |
| `relay/core.py`                  | The implementation. `FakeHubSpot` has no code changes — it is the destination and it is not mine to change.                                            |
| `tests/test_guarantees.py`       | One test class per guarantee.                                                                                                                          |

## Setup

Python 3.11 or newer. Nothing to install. SQLite ships with Python and the
HubSpot client here is a local fake, so there is no network call and no
credential to configure.

## Commands

```bash
make demo     # walks the operator's report, nine sections
make test     # 37 tests, one class per guarantee
make stress   # 8 worker processes, six checks, exits non-zero if one breaks
```

Without `make`:

```bash
PYTHONPATH=. python3 demo.py
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 stress.py
```

**`make demo` does not print a clean run, and that is the point.** The main
fixture ends `needs_review`, because the destination rewrote three of the four
approved display names and collapsed two of them into the same name.
`fixtures/deployment_request_short.json` is there to show `succeeded` is
reachable and that the rule does not fire on everything.

## Files

- `TASK.md` — the assignment.
- `fixtures/` — three approved requests of different shapes, the operator's
  report, and the event log (`c1`–`c11`).
- `relay/core.py` — the implementation and the fake HubSpot client.
- `tests/test_visible.py` — the reported failure, with assertions that now
  describe the promise rather than the symptom.
- `tests/test_guarantees.py` — one class per guarantee.
- `demo.py`, `stress.py` — what `make demo` and `make stress` run.
- `docs/diagrams/` — the diagrams, as SVG and PNG.
