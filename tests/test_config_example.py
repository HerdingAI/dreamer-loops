#!/usr/bin/env python3
"""config.example.yaml is the contract: every config key the code reads must
exist in it, so a fresh clone's `cp config.example.yaml config.yaml` is enough.

The collector is static (AST for Python, regexes for the one-liners in
bin/*.sh) and deliberately conservative: it only follows the idioms this repo
uses. The tests below pin each idiom on synthetic source, so the check cannot
pass vacuously by collecting nothing.
"""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"

# A read is (section, key). key is None when only the section is read.
Read = tuple[str, "str | None"]


class _Collector(ast.NodeVisitor):
    """Walks one module. A "scope" maps a local name to what it aliases:
    ("root",) for the whole config or ("sec", name) for one section."""

    def __init__(self) -> None:
        self.reads: set[Read] = set()
        self.scope: dict[str, tuple] = {}

    @staticmethod
    def _str(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def _step(self, base: tuple | None, key: str | None) -> tuple | None:
        """One level down from `base` by `key`; records the read."""
        if base is None or key is None:
            return None
        if base == ("root",):
            self.reads.add((key, None))
            return ("sec", key)
        if base[0] == "sec":
            self.reads.add((base[1], key))
        return None

    def resolve(self, node: ast.AST) -> tuple | None:
        if isinstance(node, ast.Name):
            if node.id == "CFG":
                return ("root",)
            return self.scope.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "CFG":
            return ("root",)  # e.g. dreamer_common.CFG
        if isinstance(node, ast.Subscript):
            return self._step(self.resolve(node.value), self._str(node.slice))
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "get" and node.args:
                return self._step(self.resolve(f.value), self._str(node.args[0]))
            if (isinstance(f, ast.Name) and f.id == "p") or \
                    (isinstance(f, ast.Attribute) and f.attr == "p"):
                key = self._str(node.args[0]) if node.args else None
                if key:
                    self.reads.add(("paths", key))
            return None
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            # `CFG.get("sec") or {}` keeps pointing at the section.
            return self.resolve(node.values[0])
        return None

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.Subscript, ast.Call)):
            self.resolve(node)
        super().generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        target = self.resolve(node.value)
        for t in node.targets:
            if isinstance(t, ast.Name):
                if target:
                    self.scope[t.id] = target
                else:
                    self.scope.pop(t.id, None)

    def _scoped(self, node: ast.AST) -> None:
        outer = dict(self.scope)
        self.generic_visit(node)
        self.scope = outer

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _scoped


def collect_python(source: str) -> set[Read]:
    c = _Collector()
    c.visit(ast.parse(source))
    return c.reads


_SL = r"yaml\.safe_load\(open\([^()]*\)\)"
_Q = r"""['"](\w+)['"]"""
_GET = r"\.get\(" + _Q
_SH_SECTIONS = [re.compile(_SL + r"\[" + _Q + r"\]"), re.compile(_SL + _GET),
                re.compile(r"\bcfg\[" + _Q + r"\]")]
_SH_KEYS = [  # (section group, key group) pairs
    re.compile(_SL + r"\[" + _Q + r"\](?:\[" + _Q + r"\]|" + _GET + ")"),
    # .get('sec', {}).get('key')  /  (.get('sec') or {}).get('key')
    re.compile(_SL + _GET + r"""(?:\s*,\s*\{\})?\)(?:\s+or\s+\{\}\))?""" + _GET),
    re.compile(r"\bcfg\[" + _Q + r"\](?:\[" + _Q + r"\]|" + _GET + ")"),
]


def collect_shell(source: str) -> set[Read]:
    reads: set[Read] = set()
    for rx in _SH_SECTIONS:
        reads.update((m.group(1), None) for m in rx.finditer(source))
    for rx in _SH_KEYS:
        for m in rx.finditer(source):
            key = next((g for g in m.groups()[1:] if g), None)
            if key:
                reads.add((m.group(1), key))
    return reads


def collect_repo(root: Path = ROOT) -> set[Read]:
    reads: set[Read] = set()
    for path in sorted((root / "scripts").glob("*.py")) + sorted((root / "bin").glob("*.py")):
        reads |= collect_python(path.read_text(encoding="utf-8"))
    for path in sorted((root / "bin").glob("*.sh")):
        reads |= collect_shell(path.read_text(encoding="utf-8"))
    return reads


def missing_from(example_path: Path, reads: set[Read]) -> list[Read]:
    example = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
    out = []
    for sec, key in sorted(reads, key=lambda r: (r[0], r[1] or "")):
        block = example.get(sec)
        if sec not in example:
            out.append((sec, key))
        elif key is not None and not (isinstance(block, dict) and key in block):
            out.append((sec, key))
    return out


class PythonIdiomsTest(unittest.TestCase):
    def test_subscript_and_get_chains(self) -> None:
        got = collect_python(
            'a = CFG["s1"]["k1"]\n'
            'b = CFG["s2"].get("k2", 3)\n'
            'c = CFG.get("s3")\n'
            'd = (CFG.get("s4") or {}).get("k4")\n'
            'e = CFG.get("s5", {}).get("k5")\n')
        self.assertLessEqual({("s1", "k1"), ("s2", "k2"), ("s3", None),
                              ("s4", "k4"), ("s5", "k5")}, got)

    def test_aliases(self) -> None:
        got = collect_python(
            "def f():\n"
            '    x = CFG["s1"]\n'
            '    y = CFG.get("s2") or {}\n'
            '    z = CFG.get("s3", {})\n'
            '    return x["a"], x.get("b"), y.get("c"), z["d"]\n')
        self.assertLessEqual({("s1", "a"), ("s1", "b"), ("s2", "c"), ("s3", "d")}, got)

    def test_attribute_root_and_path_helper(self) -> None:
        got = collect_python('cfg = C.CFG\nv = cfg["s1"]["k"]\nq = p("goals")\n')
        self.assertLessEqual({("s1", "k"), ("paths", "goals")}, got)

    def test_unrelated_dicts_are_ignored(self) -> None:
        got = collect_python('d = {}\nv = d["a"]["b"]\nw = other.get("x")\n')
        self.assertEqual(got, set())


class ShellIdiomsTest(unittest.TestCase):
    def test_one_liners(self) -> None:
        got = collect_shell(
            "M=$(py -c \"import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))"
            "['budget'].get('model') or '')\")\n"
            "L=$(py -c \"import yaml;print((yaml.safe_load(open('$ROOT/config.yaml'))"
            ".get('thread') or {}).get('fold_per_run',20))\")\n"
            "Q=$(py -c \"import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))"
            ".get('qmd',{}).get('collections'))\")\n"
            "T=$(py -c \"import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))"
            "['budget']['max_turns_nightly'])\")\n")
        self.assertLessEqual({("budget", "model"), ("thread", "fold_per_run"),
                              ("qmd", "collections"), ("budget", "max_turns_nightly")}, got)

    def test_heredoc_cfg_reads(self) -> None:
        got = collect_shell('cfg = yaml.safe_load(open(f))\nx = cfg["budget"].get("cap")\n')
        self.assertIn(("budget", "cap"), got)


class ExampleContractTest(unittest.TestCase):
    def test_collector_is_not_vacuous(self) -> None:
        reads = collect_repo()
        keyed = {r for r in reads if r[1] is not None}
        self.assertGreaterEqual(len(keyed), 40, "collector found suspiciously few keys")
        for known in [("decay", "decay_weeks"), ("matching", "recurrence_min"),
                      ("goals", "silence_ping_days"),         # via a Python alias
                      ("budget", "max_turns_assess"),         # via a shell one-liner
                      ("thread", "fold_per_run")]:            # via a shell `or {}` chain
            self.assertIn(known, reads)

    def test_every_key_the_code_reads_is_in_the_example(self) -> None:
        missing = missing_from(EXAMPLE, collect_repo())
        self.assertEqual(missing, [],
                         f"keys read by the code but absent from config.example.yaml: {missing}")

    def test_a_removed_key_is_reported(self) -> None:
        cfg = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        del cfg["decay"]["decay_weeks"]
        with tempfile.TemporaryDirectory() as d:
            alt = Path(d) / "config.example.yaml"
            alt.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            self.assertIn(("decay", "decay_weeks"), missing_from(alt, collect_repo()))


if __name__ == "__main__":
    unittest.main()
