# Documentation-level artifacts maintain Chinese and English editions as sibling files

The repo's audience is bilingual, so every documentation-level artifact (README, tutorial notebooks) is maintained as a pair: the Chinese original plus an English edition named with an `_EN` suffix (`README_EN.md`, `train_upload_EN.ipynb`, `verify_hotswap_EN.ipynb`). Image assets follow their own lowercase convention (`deploy-tab_en.jpeg`) established before this decision.

Considered options: replacing the Chinese originals with English-only files (rejected: the primary audience reads Chinese), and single bilingual files with both languages per cell (rejected: poor reading experience for both audiences and noisy diffs).

Consequences: the English edition is a 1:1 translation kept in sync in the same commit as the Chinese original — never a lagging fork; cell/section numbering (§1, §2, ...) must stay identical because both READMEs reference sections across files. Any change to a paired artifact updates both editions together, including user-visible strings in notebook code (comments, print output).
