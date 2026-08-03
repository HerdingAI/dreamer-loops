# Attribution

`SKILL.md` and `references/program.md` in this directory are vendored from
**AgriciDaniel/claude-obsidian**, MIT licensed.

- Upstream: https://github.com/AgriciDaniel/claude-obsidian
- Commit: `1c1bc49c03a685ee8f5d09c99efe52b42d6673f5`
- Vendored: 2026-08-01
- Licence: MIT — full text at `vendor/claude-obsidian.LICENSE`

## Why vendored rather than forked

See `dreamer-spec-v2.md` §6.2 "Chassis decision". In short: this is the one
capability from the upstream repo that Dreamer does not already have a tested
equivalent for. The vault, lint, lock and retrieval primitives were
reimplemented for reasons recorded there.

## Local modifications

None to these two files — they are used as upstream prompt content. Dreamer's
adaptation lives in `skills/dream/PROMPT.md`, which imports the research
contract (bounded budget, evidence ledger, source-class preference) rather than
editing the upstream text.
