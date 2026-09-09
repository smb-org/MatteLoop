# Design: In-app updates

Written 2026-09-09 against 0.3.0, for issue #74; revised the same day after an
independent review that executed the Velopack 1.2.0 wheel, the `vpk` 1.2.0
CLI and a Nuitka probe, and after the #77 spike report
Branch: feat/issue-74-in-app-updates
Repo: smb-org/MatteLoop
Status: REVIEWED — maintainer decisions folded in the same day; Phase 1 is in
implementation; Phases 2–3 are implemented and wait on the qualification gate
in "The gate" — Stage 0 and A1 passed, A2 is re-run on the rebuilt download
path. One open question remains, raised by A3: whether an elevation prompt
from the helper is acceptable for an unsigned application.

## Problem statement

A MatteLoop user learns about a new release only by visiting the releases page,
and installs it by downloading a 300 MiB archive and replacing the application
by hand. #74 asks the application to discover a newer compatible release after
startup, offer it, download it, and install it on restart — without
interrupting a Preview, Render or Rebuild, and without touching what the user
owns: model weights (up to 6.35 GiB under
`platformdirs.user_cache_dir("matteloop")`), `QSettings`, cuts and exports.

Two facts shape everything below. The application ships unsigned by decision,
so whatever installs an update must work with an ad-hoc signature on macOS and
no signature on Windows. And this repository has a measured history of
building depth nobody could walk to (`docs/engineering-guardrails.md` §1) and
of trusting artifacts nobody launched, so the design is the smallest set of
changes that delivers one complete update path, and every claim about the
packaged artifact that has not been measured is marked as something to qualify.

#74 is the requirements input for this document, not its design. Where this
document departs from a proposal in #74 it says so.

## What already exists

- **A bounded HTTP path.** `src/matteloop/ui/download_transport.py` wraps
  `QNetworkAccessManager` with the system proxy configuration, the platform
  trust store, the never-downgrade redirect policy, a 60 s inactivity timeout
  and a cancellation poll, behind the `DownloadTransport` protocol in
  `jobs/models/download.py`. The release check, the channel feed and the
  update package all go through it; it is the only network path in the
  application (Decision 3).
- **A shutdown path that waits for work.** `application.aboutToQuit` runs
  `SourceController.shutdown` (`app.py`), which closes the runtime before
  joining the workers it unblocks (#111) and joins render and transform threads
  with bounded waits. `MainWindow.closeEvent` asks about an unsaved transform
  first. Installing an update means quitting through this path. The waits are
  bounded, which matters below: shutdown returning is not proof that every
  worker has exited.
- **A job dialog that blocks the main window.** `PreviewJobDialog` is opened
  with `.open()`, which makes it window-modal (it is constructed
  application-modal, but `QDialog.open` changes that). The main window — where
  the update affordance lives — is blocked while a job runs; other top-level
  windows and other processes are not.
- **A Preferences dialog** (`ui/settings_dialog.py`) reached from the gear in
  the action shelf with the platform Preferences shortcut.
- **An update dialog and action-shelf affordance.** The update offer is a
  window-modal `UpdateDialog`; after dismissal, a painted arrow beside the
  Preferences gear reopens it. There is no inspector update banner.
- **A release workflow** (`.github/workflows/release.yml`): `native-package` on
  `macos-15` and `windows-2022`, `publish` on `ubuntu-22.04` creating a
  **draft** release that the maintainer publishes by hand. That manual step is
  the release gate today, and this design keeps it.
- **Bundle identity.** `CFBundleIdentifier` is `io.github.smb-org.matteloop`
  (`BUNDLE_IDENTIFIER` in `scripts/build.py`); the bundle and Windows version
  resources follow `matteloop.__version__` (#75, #101). `CFBundleVersion`
  cannot be written from this repository (#74 comment).
  `verify_macos_bundle_signature` in `scripts/build.py` runs `codesign -dv`
  and checks that the plist is *listed*; it displays metadata and does not
  verify a signature. `codesign --verify --deep` fails on every released bundle
  today over the plain `.py` files under `Contents/MacOS/av`, and the bundles
  launch anyway.
- **Measured so far** (#77 spike on macOS 26, this host; nothing on macOS 15
  or Windows; no Velopack feed update anywhere): after the process exits, a
  detached helper can rename an ad-hoc-signed bundle aside under
  `~/Applications`, rename a newer one into place and relaunch it by path or
  `open -a`, with no prompt, because a bundle the application wrote carries no
  quarantine attribute. A quarantined bundle opened through LaunchServices runs
  translocated from a read-only mount; its *physical* bundle can still be
  replaced, the translocated path cannot. The `velopack` wheel imports under
  Nuitka 2.8.10 and `App().run()` returns when not installed. `vpk` packs
  unsigned with warnings; its unsigned `.pkg` is rejected by the install
  assessment.

## The branch

#77 asks whether the installed bundle can be replaced and relaunched on
macOS 15, unsigned. The macOS 26 evidence predicts yes. macOS 15 is not asked
about; it is **measured on the `macos-15` runner** the workflow already uses
(Stage 0 in "The gate"), and that step can run on every build, not only during
the gate. This document is written for *yes*, and cuts rather than forks on
*no*:

- Phase 1 — the release check, the notice and *Open releases page* — lands
  either way. It is the version-check fallback named in #74 and is useful on
  its own.
- Phases 2–3 do not land. `velopack` is not added, the workflow is not
  restructured, the legal wording is not touched.
- Windows does not proceed alone. The gate is macOS-first and sequential, and
  Stage A on macOS is the go/no-go for Windows (matching #74's own order:
  qualify one complete macOS full update, then Windows).
- Package-manager publication becomes the next candidate for the manual
  install. It is not designed here.

## Decision 1 — Velopack on both platforms, qualified before it is public

**Decision.** Use Velopack (`vpk` 1.2.0 in CI, the `velopack` 1.2.0 Python
wheel at runtime) on macOS and Windows, deltas off, **and make nothing of it
public until one complete A → B update has been performed on the real Nuitka
bundle, on a real machine of each platform, through the real workflow** ("The
gate"). Velopack 1.2.0 is a candidate that has passed a wheel import and a pack
of a bare executable. It has not passed anything this project would call
verification.

**Why not bespoke.** On macOS the swap is trivial and measured. On Windows a
directory holding a running executable cannot be renamed, so the swap must run
after exit from something outside the directory; MatteLoop ships no such
executable, and the bespoke answer is a PowerShell script in `%TEMP%`, shortcut
handling and a versioned-sibling layout for the half-failed case. That is the
project writing an installer, in the territory guardrail G5 keeps it out of.
Velopack's `Update.exe` lives outside `current/` for this reason and also
supplies the feed format, version comparison, channel selection, download and
SHA-256 verification, Windows shortcuts and the uninstall entry, and deltas
when wanted.

**Why not Sparkle on macOS.** It keys on `CFBundleVersion`, which cannot be
written here, and needs an Objective-C bridge plus a second feed format.

**What Velopack 1.2.0 does not do, read from its pinned sources by the
reviewer.** These are the reasons the gate exists and are not to be assumed
away:

- *No restoration after a failed swap.* The apply helpers rename the old
  installation aside, rename the new one in, and delete both temporaries before
  propagating an error. If the second rename fails, the installation is broken;
  Windows says so in its own code. #74 asked for the working installation to
  be retained; **the maintainer has accepted this window instead**, on the
  ground that nothing the user owns lives inside the application package, so a
  manual reinstall from the releases page is always a complete recovery.
  **Measured at A3:** the window is two consecutive `rename()` calls on one
  filesystem; the extraction — the slow part — finishes before either. A
  watcher polling every 2 ms and killing the helper the instant the bundle
  vanished never hit it, and the installation came up as the new version. The
  exposure is microseconds of metadata operations, not a 380 MB copy; it gets
  one sentence in the release notes, not a warning, and the project must not
  build a journal to compensate (G3). B7 measures the same on Windows.
- *An unwritable install root ends in an elevation prompt, not a failure —
  measured at A3.* With the installation directory made read-only, the helper
  did not give up; Velopack showed "Administrator Permission Required —
  MatteLoop needs administrator permission to install version 0.3.1." The
  review had noted the helper's permission-error elevation path and the design
  treated it as theoretical. It is not, and it raises the one open question
  below.
- *"Wait for exit" is a 60 s wait that continues on timeout.* On Windows the
  helper then force-stops every process running from the install root. A
  worker that outlived the bounded shutdown waits can be killed. This is a gate
  requirement (A4, B6); the only acceptable outcome is that the helper defers
  or refuses. **Two MatteLoop instances from one installation are not
  supported** — the README says so — so the second-instance case is out of the
  gate and no single-instance mechanism is designed.
- *No signing without an identity.* `vpk` adds `UpdateMac` and `sq.version` to
  the bundle and does not re-sign it, so the packed bundle's resource seal is
  invalid (reproduced on a probe: input passed `codesign --verify`, output
  failed with "a sealed resource is missing or invalid"). Whether the full
  MatteLoop bundle still launches through the first-install route is unknown;
  today's bundles already fail deep verification and launch.
- *Its own network stack, and it is unusable behind TLS interception —
  measured.* `ureq` with `rustls` and a compiled-in root list; no system proxy
  configuration, no platform trust store, and none of `SSL_CERT_FILE`,
  `SSL_CERT_DIR`, `CURL_CA_BUNDLE`, `REQUESTS_CA_BUNDLE` honoured (all four
  tried). On the Stage A machine, behind an ordinary interception proxy whose
  root sits in the system store, every SDK request failed with `invalid peer
  certificate: UnknownIssuer` while `curl`, the browser and MatteLoop's Qt
  transport succeeded on the same network in the same second. **The SDK
  therefore neither checks nor downloads in this design** (Decision 3). That
  choice also removes three other 1.2.0 limitations the review found — no
  download cancellation, a failed feed indistinguishable from "no update", and
  staging under an auto-located cache the project does not choose — because
  the code paths that had them are no longer called.
- *Auto-location is not needed.* `VelopackLocatorConfig` lets the application
  say where its bundle, helper, manifest and packages directory are. Measured:
  with an explicit locator, a package written into the packages directory by
  something other than the SDK is reported by `get_update_pending_restart()`
  and is what the helper applies.

## Decision 2 — The user-facing flow

**When it checks.** Once per process, three seconds after `window.show()`,
only in a frozen bundle (the `sys.frozen` / `__compiled__` test
`resources.py:241` already uses; source checkouts never check at startup), and
only while *Check for updates when MatteLoop starts* is on (`updates/check_on_startup`,
default on, decided — a startup request from a tool whose smoke child forbids
networking is something the user must be able to switch off). A manual check is available
from Preferences at any time, including from source. Checks and downloads are
single-flight: a second request while one is in progress is ignored.

**Where the manual check lives.** Preferences gains a row *Updates* below
*Compute acceleration*: the beta/stable selector, the startup checkbox, a
status label and a *Check for updates* button. The selector is persisted as
`updates/channel`. Turning beta off does not downgrade an installation; it
stays on its beta until a stable release overtakes it. The application has no
menu bar and one item does not justify adding one.

**What the user sees.** When a startup check finds a newer release, the
controller opens a window-modal `UpdateDialog` naming the version and offering
the existing download/install actions or dismissal. The dialog appears once per
process and only while `store.state.job.phase is JobState.IDLE`; if a job is
already running, the offer waits until the job is idle. Dismissing it leaves a
small upward arrow beside the Preferences gear. The arrow is visible for the
Available, Downloading, Ready, and Failed states and reopens the same dialog.
**No event enters the reducer and no field is added to `AppState`**
(`core/state.py` is frozen at
882 lines; update state has no bearing on capabilities). The controller owns
the update state and reads `store.state.job.phase` where it needs it.

| State | Label | Buttons |
|---|---|---|
| Available | MatteLoop 0.4.0 is available. | Download update · Not now |
| Downloading | Downloading MatteLoop 0.4.0 (37 %)… | Cancel |
| Ready | MatteLoop 0.4.0 is ready to install. | Install and restart · Later |
| Failed | The update couldn’t be downloaded. | Try again · Open releases page |

Phase 1 ships only the *Available* state, with *Open releases page* in place
of *Download update*. It tells the user that a release exists and nothing
about their installation.

*Not now* and *Later* hide the dialog but leave the arrow available; the offer
is not persisted and the startup dialog does not repeat in one process. *Cancel*
is real: the download runs on the Qt transport, which polls a cancellation flag
exactly as the model download does.

**Download.** Only after the user asks. #74 proposed downloading in the
background first; a 300 MiB download on every launch of an install whose owner
keeps pressing *Later* is a poor default, and the answer is one click away. The
download is the feed-and-package fetch in Decision 3, run in a `WorkerThread`
with a `CancellationCheck` the transport polls, the same shape as a model
download. *Cancel*, and the controller's shutdown on `aboutToQuit`, set that
flag; the worker is then joined with the same bounded wait the other workers
get. A cancelled or failed download leaves nothing behind but a `.part` file
that is deleted; a package exists in the packages directory only after its
SHA-256 matched. The download keeps running if a job starts; it is I/O and
touches neither the segmentation process nor the work directory.

**Install.** *Install and restart* checks `store.state.job.phase is
JobState.IDLE` (the button is also disabled otherwise, refreshed from a store
subscription as `SettingsDialog.load` refreshes the provider picker), records
the pending `VelopackAsset`, and calls `window.close()`. Everything after that is
the existing quit path. The update controller's `aboutToQuit` slot — connected
in `app.py` after `SourceController.shutdown` — calls
`manager.wait_exit_then_apply_updates(pending, restart=True)`, which spawns
Velopack's helper. Arming only inside `aboutToQuit` means a close the user
cancels at the transform prompt arms nothing: after `close()` returns with the
window still visible, the pending info is dropped and the dialog returns to
*Ready*. A *Ready* state is rebuilt at the next launch from
`get_update_pending_restart()`, so *Later* loses nothing.

What this does **not** guarantee, and the gate must measure: that the helper
waits for a worker that outlives the bounded shutdown (Decision 1). The
in-process IDLE check is necessary and insufficient for that case. A second
instance rendering from the same installation is not a supported
configuration and is not defended against.

**On failure.** A failed startup check is logged at INFO and shows nothing. A
failed manual check reports in the Preferences label. A failed download is the
*Failed* state — a transport error, a checksum mismatch, or a package the SDK
does not recognise after it was written (Decision 3) — and *Try again*
repeats it while *Open releases page* is the way out. A failure inside the helper after
exit cannot be shown by this process; the next launch runs the check again,
and the dialog's *Open releases page* is the recovery path. In the
microseconds-wide case where the swap itself fails between its two renames
(Decision 1), nothing launches; the user downloads the current release and
installs it as on day one, and loses nothing, because nothing of theirs lives
in the package. One sentence in the first release notes covers it.

Before the download, advisory checks select *Open releases page* instead of
*Download update* where the outcome is known: the executable path contains
`/AppTranslocation/` (macOS), the Velopack helper and manifest are not where
the layout in Decision 3 puts them (a source run, a raw `.dist` copy), or the
install root is not writable. They are advisory in a stronger sense than "the
swap may still fail": **a failure after the download can end in an elevation
prompt from Velopack's helper** — measured at A3, a read-only install root
produced "Administrator Permission Required" rather than a failed swap. The
checks reduce the chance of reaching that prompt (a root that is unwritable
before the download is caught); they cannot prevent it (a root that becomes
unwritable between download and quit, or a permission the pre-check does not
model, is not). Whether that prompt is acceptable at all is the open question
at the end of this document; no further pre-flight is built until it is
answered.

A translocated install therefore offers the browser rather than updating
itself, and that is a deliberate simplification. #77 measured that the
*physical* bundle can be replaced while the process runs from the read-only
translocation mount — only the translocated path itself refuses, with
`Read-only file system`. Recovering that physical path needs
`SecTranslocateCreateOriginalPathForURL` from the Security framework through
`ctypes`, for a state the user leaves by moving the application once, which
the install instructions already ask for. If translocated installs turn out to
be common, that function is where the fix goes.

**Strings.** Every literal sits at its call site or in a `QT_TRANSLATE_NOOP`
table, numbers arrive through `%1`/`%2` and the presenter's `.replace("%1", …)`,
and each entry is added to both `.ts` catalogues before the phase lands
(`tests/test_translations.py`). Nothing is passed to `translate()` as a
variable.

| Context | English | German |
|---|---|---|
| UpdateBanner | Update notice *(accessible name)* | Update-Hinweis |
| UpdateBanner | Software update | Softwareupdate |
| UpdateBanner | Download size: %1 MB | Downloadgröße: %1 MB |
| UpdateBanner | MatteLoop %1 is available. | MatteLoop %1 ist verfügbar. |
| UpdateBanner | Download update | Update herunterladen |
| UpdateBanner | Open releases page | Release-Seite öffnen |
| UpdateBanner | Not now | Jetzt nicht |
| UpdateBanner | Downloading MatteLoop %1 (%2 %)… | MatteLoop %1 wird heruntergeladen (%2 %) … |
| UpdateBanner | Cancel | Abbrechen |
| UpdateBanner | MatteLoop %1 is ready to install. | MatteLoop %1 ist bereit zur Installation. |
| UpdateBanner | Install and restart | Installieren und neu starten |
| UpdateBanner | Later | Später |
| UpdateBanner | The update couldn’t be downloaded. | Das Update konnte nicht heruntergeladen werden. |
| UpdateBanner | Try again | Erneut versuchen |
| SettingsDialog | Updates | Updates |
| SettingsDialog | Check for updates when MatteLoop starts | Beim Start von MatteLoop nach Updates suchen |
| SettingsDialog | Check for updates | Nach Updates suchen |
| SettingsDialog | Checking for updates… | Nach Updates wird gesucht … |
| SettingsDialog | No update found. | Kein Update gefunden. |
| SettingsDialog | MatteLoop %1 is available. | MatteLoop %1 ist verfügbar. |
| SettingsDialog | Couldn’t check for updates. Try again later. | Updates konnten nicht geprüft werden. Versuchen Sie es später erneut. |

"No update found" is deliberate: the check can fail in ways that look like
"none", so the label never asserts that the installed version is the latest.

## Decision 3 — Version and feed model

There is one source of truth for what is released — the GitHub release — one
network path that reads it — the Qt transport — and one thing the SDK does:
apply.

**The notice comes from the API.** A stable check requests
`https://api.github.com/repos/smb-org/MatteLoop/releases/latest`; a beta check
requests `/releases` and selects the newest non-draft release. Both use the
existing Qt transport, whose `open` gains an optional `headers` keyword
(forwarded into the response and set on the request before `get`; small, but
both signatures and the call sites change). The body is capped at 1 MiB and
parsed with `json`. `tag_name` must match
`^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$`; malformed prerelease
identifiers are rejected. Newer is real semantic-version precedence against
the running Velopack version: finals outrank prereleases of the same core,
and numeric prerelease identifiers compare numerically. HTTP or parse failures
are "couldn't check", distinct from "none". This reader lives in
`src/matteloop/updates.py`, has no Qt dependency, and is the whole of Phase 1.

**The package comes through the same transport.** On *Download update*, in
the worker: fetch `releases.<channel>.json` from
`https://github.com/smb-org/MatteLoop/releases/download/v<tag>/`, where the
channel is `osx-arm64` on macOS and `win-x64` on Windows; select the asset with
`Type == "Full"` and the notice's version (the feed is Velopack's own
`{"Assets": [{PackageId, Version, Type, FileName, SHA1, SHA256, Size}]}`);
fetch `FileName` from the same release into `<packages>/<FileName>.part`
through the transport, hashing as it streams; compare with the feed's
`SHA256` (case-insensitively — `vpk` writes upper-case hex); rename into
place. The verification is the one Velopack would have performed — it moved,
it did not disappear — and the checksum came over the same trusted path as the
package. Then the **self-check**: ask `get_update_pending_restart()`; if it
does not return an asset with the expected version, the package is not where
the SDK looks or is not what it expects, the file is deleted and the dialog
shows *Failed* rather than a *Ready* it cannot honour. Measured on macOS: a
package written this way is reported as pending with no SDK network call.

**The SDK applies, and does nothing else.** `check_for_updates()` and
`download_updates()` are not called anywhere. `UpdateManager` is constructed
with an **explicit locator** derived from the running executable instead of
auto-location (which also removes the `Couldn't write out staging userId`
warning the first run produced):

| | macOS (measured) | Windows (unmeasured until Stage B) |
|---|---|---|
| `RootAppDir` | the `.app` (`executable.parents[2]`) | parent of `current\` |
| `UpdateExePath` | `Contents/MacOS/UpdateMac` | `<root>\Update.exe` |
| `ManifestPath` | `Contents/Resources/sq.version` | `current\sq.version` |
| `CurrentBinaryDir` | `Contents/MacOS` | `current\` |
| `PackagesDir` | `cache_subdirectory("updates")` | `cache_subdirectory("updates")` |
| `IsPortable` | `False` | whether the root carries Velopack's portable marker; decided at Stage B |

The `source` argument the constructor requires is the repository URL and is
never contacted. If `UpdateExePath` or `ManifestPath` does not exist, the
install is not Velopack-managed and the dialog offers *Open releases page*;
nothing is caught by exception type. The two SDK calls that remain are
`get_update_pending_restart()` (startup and self-check) and
`wait_exit_then_apply_updates()` (Decision 2).

`PackagesDir` is the project's cache, beside the model cache. At startup the
controller deletes any package there that `get_update_pending_restart()` does
not report — an applied or superseded one — so an update never leaves a 300 MiB
orphan; the SDK's own startup cleanup looks in its auto-located directory and
never sees this one.

**Stable and beta visibility is deliberate.** GitHub's `/releases/latest`
excludes drafts and prereleases, so the stable reader needs no beta filtering
from the endpoint. The beta reader uses `/releases`, skips drafts and compares
all valid release tags semantically. The feed is fetched only for the tag that
request returned. A release is complete the moment the maintainer publishes
the draft; every asset appears at once.

**Assets per release**, all on the tagged release:

- `releases.osx-arm64.json` and `releases.win-x64.json` — Velopack's feeds,
  one per channel, not renamed.
- `io.github.smb-org.matteloop-<version>-osx-arm64-full.nupkg` and the
  `win-x64` twin — the update packages, not renamed.
- `MatteLoop-v<version>-macos-arm64.zip` — the manual macOS download,
  Velopack's portable zip of the packed `.app`, renamed by `publish`.
- `MatteLoop-v<version>-windows-x64-Setup.exe` — the installed Windows
  distribution.
- `MatteLoop-v<version>-windows-x64.zip` — Velopack's portable zip. The
  maintainer keeps "copy the folder somewhere and run it" as a product
  requirement; Velopack's portable layout preserves that *and* can update
  itself through the `Update.exe` beside `current\`, which a raw Nuitka
  `.dist` copy cannot. It is a second install layout with its own A → B at
  the gate (B3). **#134 is not closed by this document.** Whether the zip
  extracts into a folder that distinguishes two releases is checked at the
  gate; if it does not, `publish` wraps it in
  `MatteLoop-v<version>-windows-x64\`, and that wrapping, verified by a
  `tests/release` test on the archive layout, is what closes #134.
- The four LGPL source archives and checksums, unchanged.

**Compatibility.** Newer is the version; compatible is the channel, which
encodes platform and architecture. No minimum-OS field is published and no
policy for moving the macOS floor is designed here; when that day comes it is
its own decision.

**Version consistency.** The workflow's `plan` job asserts on a tag push that
the numeric core of the tag matches
`src/matteloop/__init__.py`'s `__version__` and fails within seconds otherwise
— the check that would have caught the `1.0` releases. A prerelease suffix is
passed to `vpk` as the package version, while the bundle metadata remains
numeric because macOS and Windows reject the suffix there.

## Decision 4 — The release workflow

The current shape cannot simply be extended: Velopack's macOS packaging
modifies the bundle and must run on macOS, while `publish` runs on Ubuntu; the
version must be checked against the tag before two twenty-minute builds; and
qualification needs the same workflow artifacts to be downloaded and published
by hand into a temporary public test release.

- **`plan`** gains the tag-equals-version assertion (checkout plus one `grep`).
- **`workflow_dispatch`** keeps its existing platform input. The gate's two
  qualification releases are published by hand from the downloaded workflow
  artifacts with the maintainer's own `gh`; the workflow never targets another
  repository and needs no additional Actions secret.
- **`native-package`**, after "Build native standalone bundle":
  1. *Install vpk* — `dotnet tool install vpk --version 1.2.0 --tool-path .vpk`
     on the runner's .NET SDK; `DOTNET_ROOT` set explicitly, since the apphost
     does not find a Homebrew-style layout on its own.
  2. *Read the version* from the built package.
  3. *Pack.* macOS: `vpk pack --packId io.github.smb-org.matteloop
     --packVersion $VERSION --packDir dist/MatteLoop.app --mainExe matteloop
     --channel osx-arm64 --packTitle MatteLoop --noInst --delta None
     --outputDir dist/velopack`. Windows: `vpk pack` with
     `--packDir dist/MatteLoop.dist --mainExe matteloop.exe --channel win-x64
     --icon <ico> --shortcuts StartMenuRoot`, otherwise the same; both the
     installer and the portable zip are emitted.
     `--noInst` on macOS because the unsigned `.pkg` is rejected. `--delta
     None` because delta generation is on by default and this landing does not
     ship deltas; packing never fetches a base on its own — a base is a separate
     `vpk download` — so the flag is about the output, not the network. Whether
     the Windows Nuitka executable needs `--skipVeloAppCheck` is a gate item;
     the macOS command rejects that flag.
  4. *Record, do not verify, the signature.* Run `codesign --verify --deep` on
     the `.app` inside the portable zip and print the result. It is expected to
     fail (today's bundles fail it too); it is recorded so that a change in
     launchability can be correlated with a change in signature state.
     `verify_macos_bundle_signature` is not a verification and is not advertised
     as one.
  5. *Upload* `dist/velopack/` beside the existing artifacts. The pack step runs
     on every `native-package` run, so packing breakage surfaces before a tag.
- **`publish`** keeps its shape — download, set the source archives aside,
  rename the user-facing assets, `gh release create --draft` — and gains one
  assertion: both feeds and both full packages are present, so no release ever
  advertises one platform without the other. `vpk upload github` is not used;
  the repository has one release tool. The release-notes text changes with #76.

Deltas later (Phase 4): `vpk download github` before `pack`, and drop `--delta
None`. Public release assets need no token, so `contents: read` suffices.

## Decision 5 — Install layout, `packId`, and the cache

**`packId` = `io.github.smb-org.matteloop`**, the string that is already the
bundle identifier and lives in one place. `vpk` accepts it (measured).
Velopack's uninstall removes `%LocalAppData%\{packId}`; the model cache is
`%LocalAppData%\matteloop\matteloop\Cache`; the two trees share nothing and the
cache does not move. That an actual uninstall on a real machine leaves the
weights in place is a gate item.

**macOS.** `/Applications/MatteLoop.app`, moved there in Finder. The README and
release notes say so (#76): a quarantined bundle that was never moved runs
translocated, and although the spike showed its physical bundle can still be
replaced, Velopack locates the installation from the executable path and that
path is the read-only one. Updates replace the bundle in place through the
helper inside it; Velopack stages under `~/Library/Caches/velopack/<packId>/`.
The manual download stays a zip of the `.app`.

**Windows.** `Setup.exe` installs to
`%LocalAppData%\io.github.smb-org.matteloop\current\matteloop.exe` with a
Start-menu shortcut and an Apps & Features entry; updates land in the same
root. The installed path is stable, so documentation needs no version in it.
The portable zip is Velopack's portable layout — `Update.exe` beside a
`current\` directory — extracted wherever the user likes; it updates in place
from there. The documented launcher is `current\matteloop.exe` in either case.

**One instance per installation.** Running MatteLoop twice from the same
installation is not supported; the README states it. The updater relies on
it (Decision 1) and nothing enforces it.

**What never moves.** `QSettings`, the model cache, `.matteloop-work` beside
the output directory, cuts, exports. Downloaded packages live in
`cache_subdirectory("updates")` — the updater's own cache, as #74 asks,
chosen by this project and swept by it (Decision 3); an uninstall leaves it
behind the same way it leaves the weights, and it is documented in
`docs/building.md` beside the model cache.

**Existing 0.3.0 installations** get one manual step in the release notes:
macOS — replace the app in `/Applications` as before; Windows — run
`Setup.exe`, or extract the new portable zip, then delete the old extracted
`MatteLoop.dist` folder. No path they depend on changes.

## Decision 6 — Where Velopack enters the process

`packaging/entrypoint.py` is the frozen bundle's only entry. Its `else` branch
becomes:

```python
else:
    from velopack import App
    App().set_auto_apply_on_startup(False).run()
    from matteloop.app import main
    raise SystemExit(main())
```

- **Children never run it.** The resource tracker arrives with the `-c` payload
  that `_prepare_multiprocessing_payload` intercepts above this branch. A
  spawned worker arrives with `--multiprocessing-fork`, which Nuitka's own
  bootstrap recognises before any of this runs: it executes the compiled
  `__parents_main__` module and skips the `if __name__ == "__main__"` block
  entirely. No argv guard is needed and none is written.
- **It must precede `main()`.** On Windows the installer and updater start the
  executable with `--veloapp-*` hook arguments and expect `run()` to consume
  them and exit; `argparse` in `main()` would reject them.
- **Auto-apply is off.** The only place an update is ever applied is the
  `aboutToQuit` slot in Decision 2.
- **Source runs never reach it.** `uv run matteloop` enters through
  `matteloop.app:main`; the SDK is imported there only when the update
  controller constructs the manager, which raises and selects the browser path.

`velopack==1.2.0` becomes a runtime dependency (`uv lock`) and
`--include-package=velopack` joins the spec. A mocked argv test would prove
nothing about the frozen route; the entry change is verified by the gate
(worker startup from the packed bundle, the Windows hook launches).

## Decision 7 — Licence wording

Update packages are assets of the same GitHub release as the corresponding
source archives. That is the project's chosen way of satisfying GPLv3 §6(d)
(incorporated by LGPLv3 §4): source offered from the same place as the object
code, with clear directions. It is a policy, not the only lawful arrangement —
§6(d) also permits source on another server with equivalent access — and the
notices must stop presenting it as more than it is. The two texts currently
promise that binary distribution "keeps the application together with all four
adjacent source deliverables", which is already untrue of today's separate
downloads and would be conspicuously untrue of a `.nupkg`. Before Phase 2
ships a package, both change to:

> MatteLoop publishes each application and update package with access to its
> matching source companions at
> `https://github.com/smb-org/MatteLoop/releases/tag/v<version>`. The media and
> Qt source archives and checksums are separate downloads; they are not required
> to run or update the application. Packages retain the applicable licence
> notices and library-replacement instructions. Redistribution must preserve the
> applicable licences and provide the required corresponding source through a
> permitted method. For network distribution under GPLv3 §6(d), source may be
> on another server with equivalent access and clear directions beside the
> binary.

In `legal/QT-PYSIDE-LGPL-NOTICE.md` this replaces the "Binary distribution must
keep…" paragraph; in `THIRD_PARTY_NOTICES.md` it replaces the "Each successful
native build requires… together" sentence and the stale "The manual GitHub
Actions workflow only creates a temporary unsigned build artifact" paragraph.
The shipped notice carries the version-specific URL. The runtime table gains
the `velopack` SDK and the `UpdateMac` / `Update.exe` helper it places in the
bundle (MIT), with the MIT text installed beside the other licence files. The
existing spec already embeds the licence texts, the Qt notice and `RELINK.md`;
full packages carry them because they are the bundle.

## Not in scope for the first landing

- **Delta packages.** Sound in principle (#77: a delta reconstructs rather than
  patches, so the result carries the full build's own signature), payoff
  unmeasured. Phase 4 is gated on measuring the delta between two real
  consecutive releases; below a saving of half the full download it does not
  happen.
- **Package signing — out, decided.** Integrity rests on TLS plus the SHA-256
  in the feed, which is what #74 already accepted. This is distinct from
  *platform code signing* (Apple Developer ID and notarization, a Windows
  Authenticode certificate), which the project also does not do and which
  would change first-install warnings, not update integrity; the two are not
  to be conflated.
- **Background download, skip this version, automatic apply on a later
  launch, a check interval, release notes in the dialog, mirrors, a
  minimum-OS policy.**
- **Reducer or `AppState` involvement.**
- **Any MatteLoop-built rollback, journal or crash recovery** (G3). Velopack
  1.2.0 has none either; the accepted recovery is a manual reinstall
  (Decision 1).

## Phases

**#76 first**, as its own small pull request: the README and release-notes
launch instructions. Phase 1 has no technical dependency on it, but the
release notes that announce Phase 1 should not repeat a bypass that no longer
exists.

**Phase 1 — Notify.** `src/matteloop/updates.py` (API reader, tag parsing,
comparison; no Qt), `src/matteloop/ui/update_controller.py` (thread, dialog,
arrow and Preferences wiring, `QDesktopServices.openUrl`), the Preferences row,
the dialog in `MainWindow`, the transport's `headers` keyword, strings in both
catalogues, and a README sentence. The offer uses *Open releases page* in
place of *Download update* when self-update is not advisable. Startup checks
only in frozen bundles. Independent of everything below; **this is the whole
of the "no" branch.**

**Phase 2 — Package** and **Phase 3 — Install** are separate pull requests
that **share one gate and merge together or not at all.** Phase 2: the
`velopack` dependency, the spec entry, the entrypoint change, the `vpk` steps,
the tag and completeness assertions, the legal rewrite, the
`docs/building.md` additions (vpk pin, `DOTNET_ROOT`, install roots, package
cache locations). Phase 3: the feed-and-package download through the
transport, the explicit locator, the self-check, *Ready*, *Failed* and
*Cancel*, `aboutToQuit` arming, the advisory checks, the package sweep,
`MATTELOOP_UPDATE_REPO` (one environment variable, read in one place, used by
the notice request and the feed and package URLs alike). Both are developed on one qualification branch; the gate
runs on that branch's artifacts; the first public release after the merge is
the first self-updating release and its notes carry the migration step.

**Phase 4 — Deltas**, conditional on the measurement.

## The gate

Everything Decision 1 lists as unknown is answered here, before any public
distribution change. The gate is **sequential and macOS-first**: Stage A on
macOS is the decision point, and **no Windows packaging change is published
before Stage A has passed.** The material is the **actual full Nuitka bundle**
built by the **real workflow** from the qualification branch. The maintainer
downloads the workflow artifacts and publishes release A by hand in a
temporary public test repository, then does the same — from a throwaway commit
that only raises `__version__` — for release B. Installs are disposable.

**Stage 0 — the macOS 15 swap, on the runner.** `native-package` already runs
on `macos-15`. After the bundle is built and smoke-tested, a step renames
`dist/MatteLoop.app` aside, renames a second copy of it into place, executes
`Contents/MacOS/matteloop --version` directly and asserts the output. This is
an ordinary CI step, cheap enough to run on every build, and it answers #77's
question for macOS 15 without a GUI session or a Gatekeeper approval — the
update path never triggers an assessment, because a bundle the application
writes carries no quarantine attribute. What a runner cannot measure is the
*first install*, with a user approving in Privacy & Security; that is a
different question and belongs to Stage A. The caveat stays: a headless runner
is not a user's machine, and this proves the rename-and-relaunch mechanism,
not the full Velopack apply.

**Stage A — macOS. Pass here or nothing further ships.** On a real Mac:

- A1. **First install through the supported route.** Unzip, move to
  `/Applications` in Finder, approve once in Privacy & Security. Record
  `codesign --verify --deep` (expected to fail) separately from `--version`,
  `--smoke-test`, and one real Preview, which spawns the segmentation child
  from the packed bundle.
- A2. **A → B through the real updater.** Publish B; A finds it, downloads it,
  installs on quit, relaunches as B with settings, cuts and a downloaded model
  intact.
- A3. **Failed swap — measured.** Two variants on a disposable install.
  *Killing the helper between its renames:* a 2 ms poll that killed the helper
  the instant the bundle disappeared never caught the gap; the apply completed
  and the installation came up as 0.3.1. The no-rollback window is two
  adjacent `rename()` calls after extraction, accepted and sized (Decision 1).
  *Read-only install root:* the helper did not fail; it showed "Administrator
  Permission Required — MatteLoop needs administrator permission to install
  version 0.3.1." The prompt was cancelled, no elevation was granted, the
  permissions were restored, the installation was unchanged. This is a design
  finding, not a record: it contradicts the advisory-check paragraph in
  Decision 2 as first written and puts the open question below to the
  maintainer.
- A4. **Work at quit.** Quit for install with a worker blocked the way the
  #111 harness blocks one. **Requirement: the helper defers or refuses;
  nothing is killed.**
- A5. **Network.** Offline mid-download: A stays usable, the next launch shows
  no pending package. Behind an interception proxy: **measured and answered
  on the first run** — the SDK's own network path fails with `UnknownIssuer`
  and honours no CA override, the Qt transport and the browser succeed; that
  is why the SDK no longer checks or downloads (Decision 3). The re-run
  records that the rebuilt path downloads on that same machine.
- A6. **Translocation.** A quarantined copy opened via LaunchServices shows
  *Open releases page*, not a crash and not a failed swap.
- Record `PackagesDir`.

*Stage A passes* when A1 and A2 succeed exactly as written and A4 shows
deferral or refusal; A3, A5 and A6 are recorded, not scored. If A2 fails, or
A4 fails on 1.2.0 and a newer Velopack in reach does not fix it, stop at
Phase 1 on both platforms.

*Status after the first run (2026-09-09).* Stage 0 passed on the `macos-15`
runner. A1 passed: the packed bundle runs and its smoke test passes inside the
packed artifact; the notice appeared, in German, from the real feed;
`codesign --verify --deep` exits 1 as predicted, recorded. A2 did not run —
the SDK download failed before it started (A5). **Stage A is not passed.** A2
is re-run against the same two qualification releases (0.3.0 → 0.3.1) once the
download path in Decision 3 is rebuilt, and A2 now also confirms that the
helper applies a package the SDK did not download itself.

**Stage B — Windows. Reached only after Stage A passes.** On a real Windows
machine:

- B1. **First install.** `Setup.exe` through SmartScreen; `--version`,
  `--smoke-test`, one real Preview. Record whether `--skipVeloAppCheck` was
  needed to pack.
- B2. **Installed A → B** as A2.
- B3. **Portable A → B.** Extract the portable zip of A into an ordinary
  folder, run `MatteLoop-<tag>-windows-x64\current\matteloop.exe`, repeat A2.
  Measurement: Velopack's portable zip has no root directory; `publish` wraps
  `MatteLoop.exe`, `Update.exe`, `.portable` and `current\` under
  `MatteLoop-<tag>-windows-x64\`.
- B4. **Uninstall preserves the cache.** Download one model, uninstall through
  Apps & Features, confirm `%LocalAppData%\matteloop\matteloop\Cache\models`
  survives. Record `PackagesDir`.
- B5. **Hooks.** `Setup.exe` first run and the post-update relaunch pass
  through `App().run()` without `argparse` seeing `--veloapp-*`.
- B6. **Work at quit** as A4 — this is the platform whose helper force-stops
  processes in the install root.
- B7. **Failed swap** as A3, by holding a file under `current\` open.

*Stage B passes* when B1, B2, B3 and B5 succeed, B4 preserves the weights and
B6 shows deferral or refusal; B7 is recorded. Nothing about the framework is
"final" before both stages are done, and nothing Windows-related is public
before Stage A is.

## Verification

**Phase 1.** Automated: `tests/test_updates.py` with fixture JSON for a newer,
equal and older release, a tag that is not `vX.Y.Z`, a body that is not JSON,
an oversized body and an HTTP error, each mapped to "update", "none" or
  "failed". `tests/ui/test_update_controller.py` with a fake reader: dialog
hidden on "none", shown with the right text on "update", hidden after *Not
now* while the arrow remains, startup offer suppressed when the setting is off
and when a job is running, single-flight, Preferences label for each outcome,
`openUrl` with the release URL. `tests/test_translations.py`;
`tests/ui/test_preferences_surface.py` for the row and tab order. By hand: a
frozen bundle with `__version__` lowered shows the dialog within seconds on each
platform, in German too.

**Phases 2–3.** Automated, at the adapter boundaries only: the tag and
completeness assertions in `tests/release/test_ci_workflow.py`; the pinned
`vpk` version and flags; the download with a fake transport — feed parsing,
asset selection by type and version, a checksum mismatch leaves no package
and shows *Failed*, *Cancel* leaves no package, progress reaches the dialog;
the controller with a fake manager for the two calls that remain — a
self-check that returns the wrong version shows *Failed* and deletes the
file, install disabled outside `IDLE`, a refused close drops the pending
install, `aboutToQuit` arms exactly once and only when pending, the advisory
checks select the browser path, the startup sweep deletes what is not
pending; the locator derivation from a macOS and a Windows executable path.
Everything about hooks, freezing, signing, replacement, relaunch, killing and
rollback is **only** proved by the gate; a fake manager that supports invented
behaviour proves nothing, and no test is written to pretend otherwise.

**Phase 4.** The delta measurement, recorded in the issue.

## Risks that stop and re-plan

- Stage 0 fails on the `macos-15` runner — the renamed-in bundle does not
  execute (#77). The branch applies.
- Stage A fails: A2 does not update through the real feed, or A4 kills a
  stalled worker and no Velopack version in reach passes it. Stop at Phase 1
  on both platforms; Windows is never started.
- B7 shows a window on Windows larger than the two renames A3 measured on
  macOS — for instance a half-copied `current\` that launches and misbehaves.
  The accepted failure mode is "nothing launches, reinstall"; a bundle that
  starts and is wrong is a different failure and is not accepted.
- The maintainer rules the elevation prompt unacceptable and no pre-check can
  guarantee an unwritable root never reaches the apply step (A3). Then the
  helper's escalation has to be prevented at the source — a Velopack option,
  an upstream change, or not shipping the apply path — and that is a re-plan,
  not a patch.
- The packed full bundle does not launch through the first-install route
  because `vpk`'s additions broke the seal in a way the nested-file failure did
  not. Re-plan means measuring `--signAppIdentity -` against the known nested
  `.py` problem, not adding it blind.
- The helper applies only what the SDK itself downloaded. `get_update_pending_restart()`
  reporting the hand-placed package is measured; the apply of it is what the
  A2 re-run proves. If the macOS helper or the Windows one rejects it, the
  drop-in path is dead and the design goes back to Decision 1.
- The Windows locator derivation (Decision 3) is wrong, or `IsPortable` has to
  be detected in a way the table does not foresee. Measured only at Stage B.
- Frozen worker startup or the Windows hook launches misbehave with the entry
  change (A1, B1, B5).
- The guardrail ratchet: if the controller wants to reach into the reducer, the
  design is wrong, not the budget.
- API rate limiting (sixty unauthenticated requests per hour per IP) on a shared
  NAT: silent at startup, "couldn't check" on demand. Acceptable; noted so
  nobody adds a token.

## Decisions recorded from the maintainer (2026-09-09)

- The failed-rename window is accepted; recovery is a manual reinstall from
  the releases page (Decision 1). A3 has since sized it at two adjacent
  syscalls; B7 measures Windows.
- Two instances from one installation are not supported (Decision 5).
- The portable Windows distribution stays; #134 remains open until the
  archive layout is checked at the gate (Decision 3, gate item B3).
- Package signing is out, not deferred ("Not in scope").
- The qualification repository is a public one under the same organisation,
  owned by the existing account (Decision 4).
- The startup check defaults to on, with the Preferences switch; Phase 1 is
  being implemented that way.
- The macOS 15 question is measured on the `macos-15` runner (Stage 0), not
  asked; it can run as an ordinary CI step.
- The gate is sequential and macOS-first; Stage A is the go/no-go for
  Windows, and no Windows packaging change is published before it passes.
- After the first Stage A run: the SDK's network path is unusable behind TLS
  interception (measured), so the Qt transport fetches the feed and the
  package and the SDK only applies, through an explicit locator (Decisions 2
  and 3). A2 is re-run on the rebuilt path.

## Open question for the maintainer

1. **Is an administrator prompt acceptable at all from an application that
   ships unsigned by decision?** A3 measured that Velopack's helper, meeting an
   install root it cannot write, asks for elevation ("Administrator Permission
   Required — MatteLoop needs administrator permission to install version
   0.3.1") instead of failing. A user asked for their password by an unsigned
   application cannot verify what they are authorising, and refusing is the
   correct instinct. The choices:
   - (a) **Accept it** for the rare read-only-install case, and say so in the
     documentation: the prompt is Velopack's, it appears only when the
     installation directory is not writable by the user, and cancelling it
     leaves the installed version untouched (measured). The advisory pre-check
     stays as a way of making the case rarer.
   - (b) **Keep an install that cannot write its own root from ever reaching
     the apply step.** That is what the advisory check was meant to do and
     demonstrably cannot guarantee from inside the application; guaranteeing it
     means the helper must not escalate, which is a Velopack option if one
     exists, an upstream change otherwise, or not shipping the apply path for
     that case.
   The trade-off is between a rare prompt that a careful user will refuse and
   a guarantee the application cannot currently give. The design does not pick;
   nothing about the apply path is published to users before this is answered.
