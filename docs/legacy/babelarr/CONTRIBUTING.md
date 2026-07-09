# Contributing

Thanks for improving Babelarr.

## Development

Use a local virtual environment if you need one:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Run the test suite before proposing changes:

```bash
python3 -m unittest discover -s tests -v
```

For source-acquisition, output naming, job, or Runtime behavior changes, add or
update focused unit tests. Keep timeline handling in local code; translation
backends should translate cue text only.

## Secrets And Local State

Do not commit real tokens, bot IDs, profile paths, NAS hostnames, media paths,
or generated job stores. Use `.env`, ignored local docs, or your shell
environment for private deployment configuration.

Public examples should use placeholders such as `/server/media`, `/mnt/media`,
`telegram:<chat-id>`, and `replace-with-token`.

## Documentation

`README.md` is the public source of truth for user-facing commands,
requirements, and behavior changes. Keep maintainer planning notes, deployment
details, and adapter-specific runbooks in ignored local files unless they are
intentionally rewritten as public documentation.
