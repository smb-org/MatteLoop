## gstack (REQUIRED — global install)

**Before doing ANY work, verify gstack is installed:**

```bash
_GS=""
for _D in "${GSTACK_ROOT:-}" "$HOME/.claude/skills/gstack" "$HOME/.codex/skills/gstack" "$HOME/.factory/skills/gstack" "$HOME/.kiro/skills/gstack" "$HOME/.config/opencode/skills/gstack" "$HOME/.slate/skills/gstack" "$HOME/.cursor/skills/gstack" "$HOME/.openclaw/skills/gstack" "$HOME/.hermes/skills/gstack" "$HOME/.gbrain/skills/gstack" "$HOME/.gstack/repos/gstack"; do
  [ -z "$_GS" ] && [ -n "$_D" ] && [ -d "$_D/bin" ] && _GS="$_D"
done
[ -n "$_GS" ] && echo "GSTACK_OK: $_GS" || echo "GSTACK_MISSING"
```

If GSTACK_MISSING: STOP. Do not proceed. Tell the user:

> gstack is required for all AI-assisted work in this repo.
> Install it:
> ```bash
> git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
> cd ~/.claude/skills/gstack && ./setup --team
> ```
> Then restart your AI coding tool.

Do not skip skills, ignore gstack errors, or work around missing gstack.

Using gstack skills: After install, skills like /qa, /ship, /review, /investigate,
and /browse are available. Use /browse for all web browsing.
Use the resolved install path above for gstack file paths
(default: ~/.claude/skills/gstack).

## Engineering guardrails (REQUIRED — read before any code work)

**Read `docs/engineering-guardrails.md` and `docs/v1-scope.md` before writing
code, before the design document, and before the implementation plan.**

They are not style guides. They record measured failures from this repository's
own history — most importantly a hardening spiral that doubled
`jobs/workspace.py` to 5 041 lines across 14 `fix:` commits while the
application still could not open a video.

Authority order when documents disagree:

1. `docs/engineering-guardrails.md` — how to work
2. `docs/v1-scope.md` — what is in scope right now
3. `docs/designs/matteloop-desktop-app.md` — the eventual product
4. `docs/superpowers/plans/2026-08-28-matteloop-implementation.md` — historical

The three rules that get broken most often:

- **A `fix:` commit needs a `Trigger:` line** naming a repro or a
  previously-failing test. No observed trigger, no fix.
- **Third consecutive `fix:` on the same file — stop and ask the user.**
- **Commit bodies are substantive.** Never a one-liner, never test results in
  the body. See *Commit messages* below for the shape.
- **Degrade, never refuse.** A precondition the user cannot fix from the UI must
  fall back, not abort the job.

Verification for every change:

```sh
uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q
```

## Bumping `rembg` (REQUIRED — all of it, or segmentation dies silently)

The pinned rembg version is not one number. It names the model cache namespace
and is compared at three independent gates, so a partial bump leaves the app
installable, green in CI, and unable to segment a single frame. That is not
hypothetical: #18 moved the runtime 2.0.72 → 2.0.75 and nothing else, and every
preview and render on `main` failed with a bare `EOFError` until #24 — the cause
reached neither the UI nor the log (issue #22).

Change all of these together:

1. `pyproject.toml` — `rembg[cpu]==<new>`, then `uv lock`.
2. `resources/model-manifest.json` — `rembg_version`.
3. `resources/model-manifest.json` — append the **old** version to
   `obsolete_rembg_versions`. This is the step that gets forgotten. The cache
   directory is named after the pin, so every bump orphans every downloaded
   weight (up to 972 MiB each, 15 models), and that list is the only record
   those directories exist: without it the model manager cannot show, re-fetch
   or delete them, and the disk cost is invisible and permanent.

   **Append — never replace.** The list is cumulative over every version this
   tool has ever shipped, because a user may be upgrading from any of them. A
   bump to 2.0.80 makes it `["2.0.72", "2.0.75"]`; dropping an entry strands the
   weights of everyone who skipped a release, with no way back to them from the
   UI. Never migrate or rename weight directories — the namespace is the
   integrity boundary.
4. `resources/model-provenance.json` — `rembg_version` and every
   `upstream_checksum_source` path, after re-deriving each `upstream_checksum`
   from the new `rembg/sessions/*.py`. Only paths and version strings may
   change. A checksum that moved upstream is a supply-chain event, not a
   rename: stop and report it rather than committing the new value.
5. `src/matteloop/core/fingerprints.py` — `REMBG_VERSION`. `jobs/render.py`
   compares this at every render and raises `INVALID_SEGMENTATION` on drift.
6. `src/matteloop/jobs/models/catalog.py` — `_PINNED_REMBG_VERSION`.
7. `docs/building.md` — the locked-build version list.

The coupling test in `tests/jobs/models/test_catalog.py` fails when the
installed runtime and the manifest pin disagree. It is a floor, not a
substitute for this list: it cannot see steps 3, 4 or 7.

## Raising the version (REQUIRED — all of it)

`matteloop.__version__` is the source of truth, and everything a user reads
derives from it: the window title, `--version`, the first line of the startup
diagnostics, and the macOS bundle's `CFBundleShortVersionString`, which Nuitka
writes from the spec before it signs.

Two files carry the number, and the second is the one that gets forgotten:

1. `src/matteloop/__init__.py` — `__version__`.
2. `packaging/pysidedeploy.spec` — **four** version occurrences: `version` under
   `[app]`, `--file-version`, `--product-version`, and `--macos-app-version` in
   `extra_args`. The same section also carries `--macos-signed-app-name`, which
   must stay equal to `io.github.smb-org.matteloop`. Nuitka writes the Windows
   executable's version resource from the first two arguments and the macOS
   bundle's version from the latter; there is no way to derive them from the
   package at build time, so they are copies.

`CFBundleVersion` is deliberately absent: Nuitka does not write it, and adding
it after the build invalidates the ad-hoc signature — which macOS then reports
as a damaged application. That bundle cannot be re-signed either; `codesign`
refuses it over the plain `.pxd` and `.py` files inside `Contents/MacOS/av`.
Never edit a built bundle's `Info.plist`.

After changing them, run `uv sync --reinstall-package matteloop` before the
test suite: the editable install caches the version in its own metadata, and
`tests/test_app_smoke.py` compares `--version` against *that*, so it fails with
a confusing "0.3.0 != 0.2.1" until the package is reinstalled. CI syncs fresh
and never sees it. `uv.lock` does not record the project version, so it needs
no change.

`tests/release/test_version_identity.py` fails when those disagree. It is a
floor, not a substitute for this list: it cannot see the tag, the release notes,
or the README.

Then, still by hand:

3. The tag, `vX.Y.Z`, matching exactly. For a prerelease, the tag and
   `--packVersion` carry the suffix, while the bundle versions do not:
   `CFBundleShortVersionString` accepts only one to three integers and the
   Windows resource is a numeric quad. The release workflow compares the tag's
   numeric core with `__version__` and passes the full tag version to Velopack.
   The workflow is `workflow_dispatch`, so nothing enforces that the tag and
   the built version agree — a mismatch here is what shipped `1.0` in two
   releases.
4. Release notes: what is new. The README never carries version history.
5. **A minor release checks the README and the screenshots** before the bump
   (see *Keeping the README honest*). A patch needs no pass.

Never patch a version into a built artifact by hand. If a number is wrong in a
bundle, it is wrong in one of the two files above, and the fix belongs there.

## Working on issues (REQUIRED)

Work that answers a GitHub issue goes onto its own branch and into a pull
request that references the issue. Never push it to `main` directly.

- **Check the assignee before you start, then assign yourself.** An assignee is
  the only signal other people have that the work is taken; without one, two
  contributors can spend an evening on the same defect. If the issue is already
  assigned to somebody else, ask before you begin rather than working in
  parallel with them. Unassign yourself if you stop.
- Reference the issue in the pull request: `Closes #N` when the change fully
  resolves it, `Refs #N` otherwise.
- **Never merge a branch and never close a pull request.** Whether the work
  is done, and whether it lands, is the maintainer's call — not something to
  infer from green CI.
- `main` is protected; a direct push is rejected, and that is intentional.
- **Never force-push a branch that has been pushed, without being told to.**
  Amending, rebasing or squashing published commits is the maintainer's call,
  the same as merging is. `--force-with-lease` only protects against
  destroying somebody else's commits; it does not grant permission to rewrite
  history a reviewer may already have fetched, a comment may already cite, or
  a pull request already lists. Amend freely before the first push; after it,
  add a commit, or ask.

The repository is public and takes reports from other people, so the trail
from a report to the change answering it has to stay visible and reviewable.

## Static analysis on a pull request (REQUIRED)

Every pull request is analysed by **SonarCloud** and reviewed by the **Gitar
bot** alongside the test matrix, and what they report is part of the review, not
decoration. A green test suite says the code does what the tests ask. SonarCloud
reports what the tests never look at — duplicated blocks, unreachable branches,
swallowed exceptions, complexity that will be somebody's 3am page. Gitar reads
the change as a reviewer would and argues about behaviour: an affordance that
lies about what a control does, a comment that contradicts the code.

- **Read both results before proposing that anything lands.** Check the pull
  request's checks, the SonarCloud analysis link and the Gitar review comment on
  the pull request — not just the pass/fail badges. SonarCloud's check can pass
  while the quality gate reports new issues, and Gitar posts its findings as a
  review comment, so `gh pr checks` alone will not show them.
- **Fix what it finds, in the same pull request, wherever fixing it is the
  smaller change.** A finding in code this branch introduced is this branch's
  work.
- **Never merge with open findings without saying so.** If a finding is a false
  positive, or belongs to code the branch only touched in passing, say which
  finding, why it stays, and ask the maintainer before merging. Silence about a
  finding is not the same as a clean report, and "the checks were green" is not
  an answer to "what did the analysis say?".

## Commit messages (REQUIRED)

English throughout. One commit carries one complete change — the fix or feature
together with its tests, its documentation and its configuration. Do not split a
change across a commit per file, and do not commit an intermediate step that
does not stand on its own.

**Subject:** `<icon> <type>(<module>): summary`, imperative, at most 50
characters, then a blank line.

```
🐛 fix(inspector): stop Clear claiming a source it lacks
```

The module is the part of the application the change lives in — `inspector`,
`timeline`, `render`, `packaging`, `guardrails`. Leave it out when a change
genuinely spans the application.

| Icon | Type | Icon | Type |
|---|---|---|---|
| 🚀 | perf | 📝 | docs |
| ✨ | feat | 🧪 | test |
| 🛠 | improve | 🔒 | security |
| 🐛 | fix | ⚙️ | config |
| 📊 | db | 🎨 | style |
| 🔄 | refactor | ⬆️ | deps |
|  |  | 🔧 | chore |

**Body:** bullet points grouped by kind, each group led by its icon. Concrete
statements, not general ones: name the function, the value, the observed
behaviour. List only what a reader needs — compact and precise, never an essay.
Reference the issue.

```
🐛 fix(render): refuse a request whose source changed

- 🐛 Defects:
  - `_start` read the current `source_id` while the reuse probe held a
    request built from the previous one, so video A rendered under B's
    identity and its artifact was accepted as B's.
  - Selecting a cut for rebuild dispatched its transform before the user
    agreed, so cancelling left the open cut showing another cut's edits.
- 🧪 One test per defect, each failing without its fix.

Trigger: repro — start a render on a large clip, replace the source while
the reuse probe is still hashing.

Refs #60
```

**A `fix:` still needs its `Trigger:` line** (`docs/engineering-guardrails.md`),
placed after the bullets and before the issue reference.

**No attribution trailers.** No `Co-Authored-By`, no generator line, and — as
the global rule already states — never a session URL, in a commit message, a
pull request body, an issue or a comment.

## Keeping the README honest (REQUIRED)

The README is the product page, not a build note. A change a user can see is
not finished until the README shows the application as it now is.

- **A user-visible feature is not done until the README shows it.** A new
  stage, panel or output option means the README's description and its
  screenshots match what ships. A pull request that adds one and leaves the
  README describing the previous application is incomplete, the same way one
  without a test is.
- **A new user-visible string is not done until it is wrapped for Qt
  translation and present in the German catalogue.**
- **Never pass a variable to `tr()` or `translate()`.** `lupdate` extracts
  string literals, so `translate("Ctx", label)` extracts nothing: the catalogue
  keeps whatever entry happened to be there, the German interface silently
  falls back to English, and the tests stay green because they assert the
  English path. The literal has to sit at the call site — put it in
  `QT_TRANSLATE_NOOP()` where the constant is defined and translate at the
  render site, or pass a token and let the renderer choose between literals.
  Caught twice in review already: once across the inspector, the provider names
  and the transform presets, once in a disabled button's reason.
- **A minor release checks the README and the screenshots before the version
  bump.** A patch carries corrections and needs no pass. A minor carries
  something new — and the screenshots are the part that rots silently, because
  they are captured from the running application and age with every layout
  change, not only with the feature they illustrate.
- **Screenshots are generated, never collected.** `scripts/screenshots.py`
  captures them from the real widgets, so a stale one is one command away from
  being current and anyone can reproduce them. Never paste an image taken by
  hand, and never let that script reach into the application: it drives
  existing seams, and widening an API to make a screenshot easier is a change
  to the product for the benefit of a developer tool.
- **Release notes stay the place for "what is new".** The README describes the
  application as it is now and carries no version history.

Rendering the interface is also a review. The screenshots added in #43 exposed
six German strings in an English UI, a number format following the system
locale rather than the interface, and the absence of any application-level
settings surface — none of which a test, a linter or a reading review had
caught.

## Delegation

Implementation is delegated to `codex exec -m gpt-5.6-luna -c model_reasoning_effort="xhigh"`.
Second review on complex changes uses `-m gpt-5.6-sol`. Short model aliases
(`luna`, `sol`) are unavailable with a ChatGPT login and fail silently with
exit 137 — always use the full model name.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:

- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review; complex changes additionally get a Codex pass with gpt-5.6-sol (see Delegation)
- Visual polish → invoke /design-review
- Ship/PR → invoke /ship (not /land-and-deploy: merging is the maintainer's call, see Working on issues)
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
