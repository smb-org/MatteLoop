# Manage Workspaces Enablement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Manage Workspaces visibly unavailable without a READY video, explain the missing prerequisite through its tooltip and accessible description, and keep it enabled for a READY source.

**Architecture:** Derive the source-dependent action state in the pure presenter, carry it through `PresentationModel`, and render it into the Inspector alongside the existing workspace disclosure state. Keep Manage Models outside that state path because it is intentionally usable without a source.

**Tech Stack:** Python 3.13, PySide6, pytest-qt, Qt `.ts`/`.qm` translation catalogues, uv.

**Spec:** GitHub issue #89 (Option 1) and `docs/v1-scope.md` accessibility commitment.

## Global Constraints

- Do not add locks, journals, crash recovery, or inode bindings to this single-writer UI path.
- Keep the change user-reachable and limited to the observed issue; do not raise `scripts/guardrails-baseline.json`.
- New tests describe behavior, not task or review process.
- New user-visible copy must use `QCoreApplication.translate("Inspector", ...)` and exist in both checked-in catalogues and compiled `.qm` files.
- Do not commit, push, or otherwise alter git state.

---

### Task 1: Add the failing workspace-button behavior test

**Files:**
- Modify: `tests/ui/test_main_window_state_and_source_drop.py` near the existing source/action presentation tests.

**Interfaces:**
- Consumes: `MainWindow.render_state`, `AppState`, and the existing `_ready()` test fixture.
- Produces: A regression test proving the real Manage Workspaces button is disabled with an actionable reason when no source is loaded and enabled for a READY source, while Manage Models remains enabled.

- [ ] **Step 1: Write the failing test**

```python
def test_workspace_management_requires_a_ready_video(window) -> None:
    value, _ = window

    value.render_state(AppState())
    assert not value.manage_workspaces_button.isEnabled()
    assert value.manage_workspaces_button.toolTip() == (
        "Open a video to manage its workspaces."
    )
    assert value.manage_workspaces_button.accessibleDescription() == (
        "Open a video to manage its workspaces."
    )
    assert value.manage_models_button.isEnabled()

    value.render_state(_ready())
    assert value.manage_workspaces_button.isEnabled()
```

- [ ] **Step 2: Run the focused test and verify it fails for the missing state wiring**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest -q tests/ui/test_main_window_state_and_source_drop.py::test_workspace_management_requires_a_ready_video`

Expected: FAIL because the current button remains enabled with an empty tooltip and accessible description in the empty state.

### Task 2: Thread workspace availability through the presenter and view

**Files:**
- Modify: `src/matteloop/ui/presentation_model.py` to add a workspace-management enablement field.
- Modify: `src/matteloop/ui/presenter.py` to derive that field from the existing source/idle capability state.
- Modify: `src/matteloop/ui/main_window_render.py` to pass the field into the Inspector render seam.
- Modify: `src/matteloop/ui/inspector.py` to apply the state and translated unavailable reason to `manage_workspaces` only.

**Interfaces:**
- Consumes: `capabilities(state).can_edit` and the existing `Inspector.set_workspace_state(attention, open_)` seam.
- Produces: `PresentationModel.workspace_management_enabled` and `Inspector.set_workspace_state(attention, open_, enabled)`; the latter sets the button enabled state plus the same translated tooltip and accessible description when `enabled` is false, clearing the reason when it becomes true.

- [ ] **Step 1: Add the presenter model field and derive it from application state**

Add `workspace_management_enabled: bool` beside the other action flags in `PresentationModel`, and pass `allowed.can_edit` from `present()`. This makes the action available only when the application is idle with a fully READY source, matching the source requirement and existing editor capability semantics.

- [ ] **Step 2: Render the presenter-owned field into the Inspector**

Change the `main_window_render._render_actions()` call to:

```python
window.inspector.set_workspace_state(
    model.workspace_attention,
    model.workspace_open,
    model.workspace_management_enabled,
)
```

- [ ] **Step 3: Apply the button state and translated reason in the Inspector**

Change `Inspector.set_workspace_state` to accept `enabled: bool`, call `self.manage_workspaces.setEnabled(enabled)`, and set both properties with:

```python
reason = QCoreApplication.translate(
    "Inspector", "Open a video to manage its workspaces."
)
detail = "" if enabled else reason
self.manage_workspaces.setToolTip(detail)
self.manage_workspaces.setAccessibleDescription(detail)
```

Do not include `manage_models` in this logic or in the parameter-control disable list.

- [ ] **Step 4: Run the focused regression test and verify it passes**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest -q tests/ui/test_main_window_state_and_source_drop.py::test_workspace_management_requires_a_ready_video`

Expected: PASS for both empty and READY states.

### Task 3: Regenerate and compile translations

**Files:**
- Modify: `resources/matteloop_en.ts` with the extracted English source string.
- Modify: `resources/matteloop_de.ts` with the German translation `Öffnen Sie ein Video, um dessen Arbeitsbereiche zu verwalten.`.
- Modify: `resources/matteloop_en.qm` and `resources/matteloop_de.qm` through `pyside6-lrelease`.

**Interfaces:**
- Consumes: The new `Inspector` translation call and the documented Qt tooling.
- Produces: Catalogues whose extracted message keys and compiled binaries match the source.

- [ ] **Step 1: Regenerate the checked-in translation sources**

Run:

```sh
uv run --frozen --no-sync pyside6-lupdate -extensions py -no-obsolete src/matteloop -ts resources/matteloop_en.ts resources/matteloop_de.ts
```

- [ ] **Step 2: Fill the new German translation idiomatically if extraction leaves it unfinished**

Use `Öffnen Sie ein Video, um dessen Arbeitsbereiche zu verwalten.` for the new Inspector message, preserving the source key and placeholder structure.

- [ ] **Step 3: Compile the checked-in catalogues**

Run:

```sh
uv run --frozen --no-sync pyside6-lrelease resources/matteloop_en.ts resources/matteloop_de.ts
```

- [ ] **Step 4: Run the translation tests**

Run: `uv run pytest -q tests/test_translations.py`

Expected: all translation extraction, completeness, compiled-catalogue, and German-installation checks pass.

### Task 4: Run the repository verification gate

**Files:**
- Read-only verification of all changed source, test, and catalogue files.

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: Fresh command output for guardrails, lint, type checking, and the complete offscreen test suite.

- [ ] **Step 1: Run the exact required verification command**

Run:

```sh
uv run python scripts/check_guardrails.py && uv run ruff check . && uv run mypy src && QT_QPA_PLATFORM=offscreen uv run pytest -q
```

Expected: exit code 0 with all gates green and no baseline update.
