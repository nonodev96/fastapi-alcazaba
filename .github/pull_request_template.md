## What and why

<!-- What does this change, and what problem does it solve? Link the issue if there is one. -->

## Checklist

- [ ] Tests that fail without the change (unit, plus integration/e2e if it touches a backend)
- [ ] `.venv/bin/pre-commit run --all-files` passes
- [ ] `CHANGELOG.md` updated (if users will notice)
- [ ] Docs updated (README / docs/)
- [ ] Secure defaults kept: nothing new can serve one user's data to another, trust backend data
      or let clients bypass the cache unless it's opt-in
