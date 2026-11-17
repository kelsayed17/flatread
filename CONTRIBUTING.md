# Contributing

## Setup

flatread needs Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Checks

Every change must pass all of these, which CI runs on every pull request:

```bash
uv lock --check
uv run ruff check
uv run ruff format --check
uv run ty check
uv run deptry src
uv run lint-imports
uv run typos
uv run python -m pytest --cov
```

CI also runs the tests at the lowest declared dependency versions, imports each module with only its own extra
installed, and audits the dependencies and the workflows.

- **Strictness is the point.** A change must not make a reader return a value the file does not hold: no silent
  `None`, no padded short row, no rounded digit. A file a reader cannot read exactly is an error that names where.
- **Fix the cause of a lint or type finding** rather than suppressing it.
- **A new branch needs a test.** Coverage is held at the threshold in CI.

## Pull requests

Keep a pull request to one change, and describe the behavior it changes for someone reading a file with flatread.
A change that breaks an existing reader's behavior needs a major version and a migration note in the README.
