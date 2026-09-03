# Contributing to YanBi 言笔

Thanks for your interest in contributing. The project is intentionally small — a single-file GUI (`voice_input.py`) plus a self-contained ASR module (`volc_asr.py`) — so the bar for changes is "keep it simple".

## Ground rules

- **Search before reporting.** Check open issues (and Discussions) before filing a bug or feature request to avoid duplicates.
- **Discuss before big PRs.** For anything beyond a small bug fix, open an issue first and describe what you plan to change and why, so the direction is agreed before you invest time.
- **Follow the existing structure.** The code is deliberately kept as single-file modules; keep new code in the same shape rather than introducing a framework or heavy abstraction.
- **Attribution is handled by maintainers.** Do not add your own author line or `Co-authored-by` trailer; the maintainer manages credit and the contributor list.

## Pull request checklist

- Target the `main` branch.
- Keep the diff scoped to one change; no unrelated reformatting.
- Match the surrounding code's style over your own defaults.
- Make sure `python -m py_compile voice_input.py volc_asr.py` still passes.

## Issues

Issue templates are available for [bug reports](.github/ISSUE_TEMPLATE/bug_report.md) and [feature requests](.github/ISSUE_TEMPLATE/feature_request.md). You may write in Chinese if that's easier.

---

### 中文要点（贡献须知）

- 提 issue 前先搜索，避免重复。
- 较大的改动请先开 issue 讨论方向，再提 PR。
- 代码保持现有单文件结构，不要引入框架或过度抽象。
- 署名由维护者统一处理，PR 里不要自己加署名。
