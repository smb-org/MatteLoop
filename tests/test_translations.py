from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest
from PySide6.QtCore import QLibraryInfo
from PySide6.QtWidgets import QDialogButtonBox

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_PLACEHOLDER = re.compile(r"%(?:n|[0-9]+|s)")


def _placeholders(text: str) -> Counter[str]:
    return Counter(_PLACEHOLDER.findall(text))


def _message_keys(path: Path) -> set[tuple[str, str, bool]]:
    root = ET.parse(path).getroot()
    return {
        (
            context.findtext("name") or "",
            message.findtext("source") or "",
            message.attrib.get("numerus") == "yes",
        )
        for context in root.findall("context")
        for message in context.findall("message")
    }


def _assert_translations_are_complete(path: Path) -> None:
    root = ET.parse(path).getroot()
    messages = list(root.iter("message"))
    assert messages
    for message in messages:
        source = message.findtext("source") or ""
        translation = message.find("translation")
        assert translation is not None
        if message.attrib.get("numerus") == "yes":
            forms = translation.findall("numerusform")
            assert forms and all(form.text for form in forms), source
            translated = [form.text or "" for form in forms]
        else:
            assert (translation.text or "").strip(), source
            translated = [translation.text or ""]
        for form in translated:
            assert _placeholders(source) == _placeholders(form), source


def test_catalogues_match_lupdate_extraction(tmp_path: Path) -> None:
    lupdate = shutil.which("pyside6-lupdate")
    if lupdate is None:
        pytest.skip("pyside6-lupdate is not on PATH")
    generated = tuple(
        tmp_path / name for name in ("matteloop_en.ts", "matteloop_de.ts")
    )
    subprocess.run(
        [
            lupdate,
            "-extensions",
            "py",
            "-no-obsolete",
            "src/matteloop",
            "-ts",
            *(str(path) for path in generated),
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
    )
    for filename, extracted in zip(
        ("matteloop_en.ts", "matteloop_de.ts"), generated
    ):
        checked = REPOSITORY_ROOT / "resources" / filename
        assert _message_keys(extracted) == _message_keys(checked)
        _assert_translations_are_complete(checked)


def test_compiled_catalogues_match_their_sources(tmp_path: Path) -> None:
    """A stale .qm ships the wrong words while every .ts review looks correct."""
    lrelease = shutil.which("pyside6-lrelease")
    if lrelease is None:
        pytest.skip("pyside6-lrelease is not on PATH")
    for name in ("matteloop_en", "matteloop_de"):
        rebuilt = tmp_path / f"{name}.qm"
        subprocess.run(
            [
                lrelease,
                str(REPOSITORY_ROOT / "resources" / f"{name}.ts"),
                "-qm",
                str(rebuilt),
            ],
            check=True,
        )
        checked = REPOSITORY_ROOT / "resources" / f"{name}.qm"
        assert rebuilt.read_bytes() == checked.read_bytes(), name


def test_installing_translators_reaches_the_german_catalogue() -> None:
    """app.py calls this at every GUI start; nothing else covers it."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    from matteloop.ui.i18n import install_translators

    application = QApplication.instance() or QApplication([])
    assert isinstance(application, QApplication)
    translators = install_translators(application, "de")
    try:
        assert translators
        assert (
            QCoreApplication.translate("ActionShelf", "Render Video")
            == "Video rendern"
        )
    finally:
        for translator in translators:
            application.removeTranslator(translator)


def test_composed_status_copy_is_translated_from_the_checked_german_qm() -> None:
    from PySide6.QtCore import QTranslator
    from PySide6.QtWidgets import QApplication

    from matteloop.ui.copy import presented_status_copy

    application = QApplication.instance() or QApplication([])
    translator = QTranslator()
    catalogue = REPOSITORY_ROOT / "resources" / "matteloop_de.qm"
    assert translator.load(str(catalogue))
    application.installTranslator(translator)
    try:
        assert presented_status_copy(
            "Settings changed — preview again", "Crop & cleanup"
        ) == (
            "Einstellungen geändert — Vorschau erneut anzeigen · "
            "Zuschnitt und Bereinigung"
        )
    finally:
        application.removeTranslator(translator)


def test_update_copy_is_translated_from_the_checked_german_qm() -> None:
    from PySide6.QtCore import QCoreApplication, QTranslator
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    translator = QTranslator()
    catalogue = REPOSITORY_ROOT / "resources" / "matteloop_de.qm"
    assert translator.load(str(catalogue))
    application.installTranslator(translator)
    try:
        assert QCoreApplication.translate(
            "UpdateBanner", "MatteLoop %1 is available."
        ) == "MatteLoop %1 ist verfügbar."
        assert QCoreApplication.translate(
            "SettingsDialog", "Couldn’t check for updates. Try again later."
        ) == "Updates konnten nicht geprüft werden. Versuchen Sie es später erneut."
    finally:
        application.removeTranslator(translator)


def test_render_stages_are_translated_at_the_dialog_boundary() -> None:
    from PySide6.QtWidgets import QApplication

    from matteloop.core.tokens import ProgressStage
    from matteloop.ui.i18n import install_translators
    from matteloop.ui.preview_controller.dialog import _stage_copy

    application = QApplication.instance() or QApplication([])
    translators = install_translators(application, "de")
    expected = {
        ProgressStage.PREPARING_MODEL: "Modell wird vorbereitet",
        ProgressStage.DOWNLOADING_MODEL: "Modell wird heruntergeladen",
        ProgressStage.SEGMENTATION: "Segmentierung",
        ProgressStage.DECODE: "Dekodierung",
        ProgressStage.RENDER_CUT: "Dekodierung",
        ProgressStage.FRAMING: "Rahmung",
        ProgressStage.CUT_PROMOTION: "Schnittbilder übernehmen",
        ProgressStage.VALIDATION: "Validierung",
        ProgressStage.POST_PROCESS: "Nachbearbeitung",
        ProgressStage.AUTO_FIT: "Automatische Anpassung",
        ProgressStage.ENCODE: "Kodierung",
        ProgressStage.VALIDATE: "Validierung",
        ProgressStage.COMPLETE: "Abgeschlossen",
        ProgressStage.CANCELLING: "Wird abgebrochen…",
    }
    try:
        assert {stage: _stage_copy(stage) for stage in expected} == expected
        assert _stage_copy(ProgressStage.AUTO_FIT, 3, 12) == (
            "Automatische Anpassung, Versuch 3 von maximal 12"
        )
    finally:
        for translator in translators:
            application.removeTranslator(translator)


def test_frozen_layout_translates_qt_close_button_from_its_qt_catalogue(
    tmp_path: Path,
) -> None:
    from PySide6.QtWidgets import QApplication

    from matteloop.ui.i18n import install_translators

    application = QApplication.instance() or QApplication([])
    resources = tmp_path / "resources"
    resources.mkdir()
    checked_resources = REPOSITORY_ROOT / "resources"
    shutil.copy2(checked_resources / "matteloop_de.qm", resources)
    qt_translations = (
        tmp_path / "PySide6" / "Qt" / "translations"
    )
    qt_translations.mkdir(parents=True)
    installed_qt = Path(
        QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    )
    shutil.copy2(installed_qt / "qtbase_de.qm", qt_translations)

    translators = install_translators(application, "de", runtime_root=tmp_path)
    try:
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button = button_box.button(QDialogButtonBox.StandardButton.Close)
        assert button is not None
        assert button.text() == "Schließen"
    finally:
        for translator in translators:
            application.removeTranslator(translator)
