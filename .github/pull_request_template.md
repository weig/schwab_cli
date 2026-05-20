## Summary

<!-- One paragraph: what changed and why. -->

## Test plan

<!-- Tick every box before requesting review. The PR Checklist workflow
     enforces this — unticked boxes block merge. Drop any line that
     genuinely doesn't apply rather than leaving it unticked. -->

- [ ] `uv run pytest` is green locally
- [ ] `uv run ruff check .` is clean
- [ ] `gitleaks detect --no-git` clean (or new placeholders allowlisted in `.gitleaks.toml`)
- [ ] New / changed behaviour covered by tests
- [ ] User-visible changes documented (README / `doc/`)
- [ ] No new secrets, tokens, or hardcoded credentials in the diff
