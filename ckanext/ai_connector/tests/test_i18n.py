"""Every interface text is translated in the maintained catalogues, and every
compiled catalogue matches its source."""

import os
import subprocess
import sys
from pathlib import Path

from babel.messages.mofile import read_mo
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parents[3]
I18N = ROOT / "ckanext" / "ai_connector" / "i18n"
MAINTAINED_LANGUAGES = ("sv", "sv_SE")


def test_every_extracted_string_is_translated(tmp_path):
    pot = tmp_path / "extracted.pot"
    env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.run([sys.executable, "-m", "babel.messages.frontend", "extract",
                    "-F", "babel.cfg", "-o", str(pot), "."],
                   cwd=str(ROOT), env=env, capture_output=True, text=True, check=True)
    with pot.open("rb") as f:
        extracted = {m.id for m in read_po(f) if m.id}
    assert len(extracted) > 30
    for language in MAINTAINED_LANGUAGES:
        with (I18N / language / "LC_MESSAGES" / "ckanext-ai-connector.po").open("rb") as f:
            translated = {m.id for m in read_po(f) if m.id and (
                all(m.string) if isinstance(m.string, tuple) else m.string)}
        assert not sorted(extracted - translated, key=str), language


def _key(value):
    return tuple(value) if isinstance(value, (list, tuple)) else value


def test_every_catalogue_is_compiled_and_up_to_date():
    for po_path in I18N.glob("*/LC_MESSAGES/*.po"):
        with po_path.open("rb") as f:
            po = {_key(m.id): _key(m.string) for m in read_po(f) if m.id and m.string and not m.fuzzy}
        with po_path.with_suffix(".mo").open("rb") as f:
            mo = {_key(m.id): _key(m.string) for m in read_mo(f) if m.id}
        assert po == mo, po_path.parent.parent.name


def test_sv_SE_is_a_copy_of_sv():
    def messages(language):
        with (I18N / language / "LC_MESSAGES" / "ckanext-ai-connector.po").open("rb") as f:
            return {_key(m.id): _key(m.string) for m in read_po(f) if m.id}
    assert messages("sv_SE") == messages("sv")
