"""The suite must never reach the owner's work folder or settings (it once deleted job files)."""
import os
import tempfile

import conftest
import workspace


def test_the_suite_has_its_own_work_folder():
    assert workspace.WORK_DIR != os.path.abspath(conftest.SRC.parent / "work")
    assert os.path.realpath(workspace.WORK_DIR).startswith(os.path.realpath(tempfile.gettempdir()))


def test_every_setting_the_code_reads_is_set_before_any_import():
    import re
    read = set()
    for source in conftest.SRC.glob("*.py"):
        read |= set(re.findall(r"environ(?:\.get\(|\[)\s*[\"']([A-Z_]+)", source.read_text()))
    assert read - {"PATH"} <= set(conftest.ISOLATED)
