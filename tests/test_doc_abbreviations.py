"""Every document ends with an up-to-date "Abbreviations Used in this Document" appendix."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("doc_abbreviations", ROOT / "tools" / "doc_abbreviations.py")
abbr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(abbr)


def test_every_document_defines_its_abbreviations(capsys):
    assert abbr.main([]) == 0, capsys.readouterr().err


def test_definitions_use_only_defined_abbreviations():
    _, _, unknown = abbr.scan_prose(" ".join(abbr.GLOSSARY.values()))
    assert not unknown


def test_an_undefined_abbreviation_is_caught_and_code_is_not():
    text = "The XYZQ journal and NTFS. `ABCD` in code.\n\n```\nEFGH\n```\n"
    used, _, unknown = abbr.scan(text)
    assert unknown == {"XYZQ"}
    assert used == {"NTFS"}


def test_the_appendix_lists_what_is_used_and_what_its_definitions_use():
    out = abbr.render("Uses exFAT and R-RO-01.\n")
    assert "## Abbreviations Used in this Document" in out
    for term in ("| exFAT |", "| USB |", "| SD |", "| R-…-nn |", "| FAIR |", "| NDSA |"):
        assert term in out
    assert abbr.render(out) == out          # regenerating is stable


def test_a_paragraph_in_capitals_is_not_read_as_abbreviations():
    text = ("THE SOFTWARE IS PROVIDED AS IS, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.\n\n"
            "A normal paragraph naming the USN journal and an XYZQ.\n")
    used, _, unknown = abbr.scan(text)
    assert used == {"USN"}
    assert unknown == {"XYZQ"}
