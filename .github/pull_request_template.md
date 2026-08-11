## What this changes

<!-- What does it do, and why is it worth doing? -->

## How it was checked

<!-- Which of these you ran, and anything that needed a live service. -->

- [ ] `cargo fmt --all --check`
- [ ] `cargo clippy --locked --workspace --all-targets -- -D warnings`
- [ ] `cargo test --workspace`
- [ ] `ruff check nitro tests` and `ruff format --check nitro tests`
- [ ] `pytest`

## Notes for the reviewer

<!--
Anything that would be hard to see from the diff: a decision that had
alternatives, something deliberately left out, a behaviour change a project
upgrading would notice.
-->

- [ ] A behaviour change is described in `CHANGELOG.md`
- [ ] A fix comes with a test that fails without it
