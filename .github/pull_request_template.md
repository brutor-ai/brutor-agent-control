## What this changes

## Why

## How it was tested

<!-- Say what you ran, on which commit, and what it showed. If you could not run something, say so. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`, Developer Certificate of Origin).
- [ ] A bug fix has a test that failed before the fix.
- [ ] No test, assertion, lint, dependency rule or timeout was removed or loosened to make this pass. If one was changed, the reason is stated above.
- [ ] No lint cap was raised (`--max-warnings` in `admin-console/package.json`, `core-gateway/scripts/lint-ratchet.baseline`).
- [ ] No Enterprise code is imported and no Enterprise-only table is read. If something named in `editions.yaml` changed, `editions.yaml` is updated and `check_editions.py` passes.
- [ ] A schema change has a migration under `control-plane/alembic/versions/`.
- [ ] No secret, stock password or personal data is in the change.
- [ ] New source files start with the copyright and SPDX header.
- [ ] This touches a Critical file of the Core Gateway (see CONTRIBUTING.md): yes / no. If yes, there is a test for the failure path.

Gates run for each component touched:

- [ ] Core Gateway: `cargo fmt --all -- --check`, `cargo clippy --locked --workspace --all-targets --all-features -- -D warnings`, `cargo deny --locked check`, `scripts/lint-ratchet.sh`, `cargo bench --no-run --locked`, `cargo nextest run --locked --workspace --lib --tests --features db-tests`
- [ ] Control Plane: `BRUTOR_TEST_EDITION=community pytest --cov=app tests/`
- [ ] Admin Console: `npm run type-check`, `npm run lint`, `npm run test:run`, `npm run build`, `npm run scan:bundle`
- [ ] Skill Runner: `cargo fmt --all -- --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test --all-targets`, `cargo audit`
