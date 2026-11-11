# Coder tools: repo index, patch and git

M2 coder tooling adds three read/write surfaces to the tool layer:

- a read-only index of the workspace (`repo.index`);
- an exact-text patch tool (`code.patch`);
- a fixed allowlist of git operations (`git.status`, `git.diff`, `git.branch`,
  `git.commit`).

All of them go through `ToolExecutor`, declare a risk and a capability, and are
audited like every other tool. `code.patch` reuses the `fs.write` capability;
git tools never accept an arbitrary argv list.

## Tool table

| Tool | Risk | Capability | What it does |
|---|---|---|---|
| `repo.index` | R0 | `repo.read` | Summary, case-insensitive search and symbol list over workspace text files. |
| `code.patch` | R1 | `fs.write` | Replace an exact, counted occurrence in one UTF-8 file after a unified-diff preview. |
| `git.status` | R0 | `git.read` | `git status --porcelain=v1 --branch`. |
| `git.diff` | R0 | `git.read` | `git diff --stat` plus an optional focused `git diff -- <path>`. |
| `git.branch` | R2 | `git.branch` | `git branch --show-current` or `git switch -c <name>`. |
| `git.commit` | R3 | `git.commit` | `git add -- <paths>` followed by `git commit -m <message>`. |

R2 and R3 need approval from the executor. R0/R1 run automatically in
`A4-Bounded`; lower autonomy modes tighten that table without changing the
tools.

## Repository index (`repo.index`)

`RepoIndex(workspace, *, max_files=5000, max_bytes=2_000_000)` scans the
workspace and builds `RepoFile` rows with `path`, `lines`, `bytes`, `language`
and `mtime`. Language is selected by suffix (`.py` → `python`, `.ts`/`.tsx` →
`typescript`, `.md` → `markdown`, unknown text extensions → `text`).

Scan rules:

- the workspace is walked without following symlinks or junctions;
- these directories are skipped at any depth: `.git`, `data`, `private`,
  `voice-dataset`, `.venv`, `node_modules`, `__pycache__`;
- files with binary suffixes (images, archives, executables, model weights,
  Office/PDF, media, `.pyc`, databases) are skipped;
- files larger than `max_bytes` (2 MB by default) are skipped;
- files that are not valid UTF-8 or contain a NUL byte are skipped;
- secret-looking names (`.env`, `*.pem`, `*.key`, `id_rsa*`, …) are skipped;
- at most `max_files` (5000 by default) text files are indexed; a partial index
  is reported as `index_truncated`.

Queries:

- `search(query, limit=20)` is a case-insensitive substring match over paths and
  file contents. Content hits carry a line number and a trimmed snippet; path
  hits are reported as `(path match)`.
- `symbols()` uses line regexes: Python `class`/`def` (including `async def`) and
  JavaScript/TypeScript `class`/`function` (including `export` and `async`).
  This is a heuristic, not a parser.
- `summary()` returns totals plus file counts by language.

The tool has no path argument: it always indexes `ctx.workspace`. It performs no
writes and returns content as `untrusted:file`.

Example:

```powershell
kivi tools run repo.index --args '{"action":"summary"}'
kivi tools run repo.index --args '{"action":"search","query":"PatchArgs","limit":10}'
kivi tools run repo.index --args '{"action":"symbols","limit":50}'
```

## Patch semantics (`code.patch`)

Arguments:

```json
{"path": "src/kivi/example.py", "old": "VALUE = 1", "new": "VALUE = 2", "count": 1}
```

Rules, applied before anything is written:

1. `old` must be non-empty and must differ from `new`; `new` may be empty
   (deletion).
2. The path goes through Protected Zones exactly like `fs.write`: workspace
   only, no `.git`, `private/`, `voice-dataset/`, safety core, config, audit
   data or secret files.
3. The target must be an existing regular UTF-8 file.
4. The file and the patched result must fit `ctx.limits.max_write_bytes`
   (256 KiB by default).
5. `old` must occur exactly `count` times (default 1). A mismatch is refused, so
   a stale file model cannot silently change the wrong location.
6. `old == new` is refused even when `count` matches.

`dry_run=True` returns a `Preview` whose details include the unified diff; the
file is untouched. A real run repeats the same validation, writes a temporary
file in the target directory, replaces the target atomically with `os.replace`,
and returns a `ToolResult` with `occurrences`, `bytes_before`, `bytes_after`,
`added`, `removed` and the full `diff` in `meta`. The result is
`untrusted:tool`.

The tool is not a fuzzy patcher: there is no context matching, no line-range
mode and no `overwrite` flag. Read the file, build exact text, patch once.

Example:

```powershell
kivi tools run code.patch --args '{"path":"docs/note.md","old":"draft","new":"final"}'
kivi tools run code.patch --args '{"path":"docs/note.md","old":"draft","new":"final"}' --dry-run
```

## Git allowlist

Every operation builds a fixed argv list. The only commands that can run are:

```text
git status --porcelain=v1 --branch
git diff --stat
git --literal-pathspecs diff -- <path>
git branch --show-current
git switch -c <name>
git --literal-pathspecs add -- <paths>
git commit -m <message>
```

There is no field for an arbitrary command, subcommand or extra git option. Git
runs with `shell=False`, `cwd`/`-C` set to the workspace, a minimal environment,
a 30-second timeout and capped output. Path-bearing commands use
`--literal-pathspecs`, so a path such as `notes[1].md` or `**` is staged
literally and cannot broaden into a glob that picks up secret files.

Validation:

- branch names match `^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$`, cannot contain
  `..`, `//`, `@{`, cannot end in `/`, `.` or `.lock`, and no component may start
  with `.`. This blocks option injection such as `-D` and ref tricks.
- commit messages must be non-empty, whitespace-trimmed and at most 200
  characters.
- paths are workspace-relative, cannot be absolute, cannot contain `..`, cannot
  contain a `.git` component and cannot resolve to the workspace root. Protected
  Zones are applied as well.
- `git.commit` stages only the listed paths. It does not amend, push, reset,
  force, rebase, tag, change hooks, or bypass hooks. Pre-existing staged changes
  are not isolated by this tool; use explicit paths.

`git.branch` (R2) and `git.commit` (R3) return `confirmation required` until the
caller approves them. `git.status` and `git.diff` are R0.

Examples:

```powershell
kivi tools run git.status
kivi tools run git.diff --args '{"path":"src/kivi/tools/patch.py"}'
kivi tools run git.branch --args '{"action":"current"}' --approve
kivi tools run git.branch --args '{"action":"create","name":"feat/coder-tools"}' --approve
kivi tools run git.commit --args '{"message":"feat: coder tools","paths":["src/kivi/tools/patch.py"]}' --approve
```

## Registration and grants

The default registry (`build_registry()`) must register the new tools and tasks
must request the matching capabilities:

```python
from kivi.tools.git import GitBranchTool, GitCommitTool, GitDiffTool, GitStatusTool
from kivi.tools.patch import CodePatchTool
from kivi.tools.repo import RepoIndexTool

for tool in (
    RepoIndexTool(),
    CodePatchTool(),
    GitStatusTool(),
    GitDiffTool(),
    GitBranchTool(),
    GitCommitTool(),
):
    registry.register(tool)
```

Capabilities: `repo.read`, `fs.write` (patch), `git.read`, `git.branch`,
`git.commit`. A grant without the matching capability is denied by the
Permission Engine before the tool runs.

## Limitations

- No push, pull, fetch, clone, remote management or network access.
- No `--amend`, `--no-verify`, `--force`, reset, rebase, cherry-pick, tag,
  stash, merge or worktree commands.
- No arbitrary git command or extra argv; new operations need a new typed tool.
- `git.commit` commits the explicit paths it stages. Files already staged by
  another process are also picked up by the commit; the tool does not provide a
  clean index transaction.
- The repo index is a substring scanner, not a symbol server: it does not parse
  imports, resolve types or follow generated/build directories.
- `code.patch` handles UTF-8 text only; binary files, files above the write cap
  and files outside the workspace are refused.
- Git hooks are neither run by tests nor bypassed by the tool; the commit uses
  the repository's normal configuration.
