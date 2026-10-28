#!/usr/bin/env python3
"""Egress gate: the query check and the PreToolUse hook."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import egress_check as EC  # noqa: E402

HOOK = ROOT / "scripts" / "egress_hook.py"

# Tiny dictionary standing in for /usr/share/dict/words: it lists "dana" and
# "denver" capitalised (names), "march" both ways, and generic words lowercase.
DICT = """\
what
is
the
best
way
to
learn
about
in
how
do
people
compare
kiln
shelves
glazes
recipes
Dana
Denver
March
march
Marlow
marlow
a
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.dict_path = self.tmp / "words"
        self.dict_path.write_text(DICT, encoding="utf-8")
        self.blocked_path = self.tmp / "blocked.txt"
        self.blocked_path.write_text("# names\nJane Roe\nAcme Holdings\n", encoding="utf-8")

    def gate(self, **kw):
        args = dict(dictionary_path=self.dict_path, blocked_names_path=self.blocked_path,
                    allowed_proper_nouns=["Python"], phrase_run_words=4,
                    year_min=1900, year_max=2100)
        args.update(kw)
        return EC.Gate(**args)


class QueryCheckTest(Base):
    def test_generic_question_with_a_year_is_allowed(self):
        r = self.gate().check("What is the best way to learn about kiln shelves in 2026")
        self.assertTrue(r.ok, r.reasons)

    def test_blocked_name_any_case(self):
        for q in ("how do people compare jane roe", "JANE ROE glaze recipes",
                  "best way to learn about Acme Holdings"):
            r = self.gate().check(q)
            self.assertFalse(r.ok, q)
            self.assertTrue(any("blocked name" in x for x in r.reasons))

    def test_blocked_name_is_a_whole_term_match(self):
        self.blocked_path.write_text("Jan\n", encoding="utf-8")
        self.assertTrue(self.gate().check("how do people compare january kiln shelves").ok)

    def test_dictionary_name_not_allowlisted_is_blocked(self):
        r = self.gate().check("what is the best way to learn about Dana")
        self.assertFalse(r.ok)
        self.assertTrue(any("Dana" in x for x in r.reasons))

    def test_capitalised_word_also_lowercase_in_dictionary_still_blocked(self):
        # "March" is capitalised in the dictionary: an entry never proves it generic.
        self.assertFalse(self.gate().check("how do people compare March").ok)

    def test_lowercase_name_the_dictionary_lists_only_capitalised_is_blocked(self):
        # skills/frontier-queries/PROMPT.md mandates all-lowercase queries
        for q in ("how does dana negotiate", "how do people compare denver"):
            r = self.gate().check(q)
            self.assertFalse(r.ok, q)
            self.assertTrue(any("capitalised in the dictionary" in x for x in r.reasons), r.reasons)

    def test_lowercase_word_listed_both_ways_or_absent_passes(self):
        self.assertTrue(self.gate().check("how do people compare march").ok)  # also lowercase-listed
        self.assertTrue(self.gate().check("what is celadon").ok)  # a technical term the dictionary lacks
        self.assertTrue(self.gate().check(
            "how much glaze thickness is lost during slow-cooled firing").ok)

    def test_allowed_proper_noun_lifts_the_lowercase_check_too(self):
        self.assertTrue(self.gate(allowed_proper_nouns=["Dana"]).check("how does dana negotiate").ok)

    def test_non_ascii_letters_are_blocked_unless_allowed(self):
        for q in ("how does émilie negotiate", "how does müller negotiate", "what is the best way to learn Émilie"):
            r = self.gate().check(q)
            self.assertFalse(r.ok, q)
            self.assertTrue(any("non-ascii" in x for x in r.reasons), r.reasons)
        self.assertTrue(self.gate(allowed_proper_nouns=["müller"]).check("how does müller negotiate").ok)

    def test_allowed_proper_noun_passes_case_insensitively(self):
        self.assertTrue(self.gate().check("best way to learn Python").ok)
        self.assertTrue(self.gate().check("best way to learn PYTHON").ok)

    def test_capitalised_unknown_token_is_blocked(self):
        self.assertFalse(self.gate().check("best way to learn about Zorblax").ok)

    def test_sentence_initial_generic_word_is_fine(self):
        self.assertTrue(self.gate().check("How do people compare kiln shelves").ok)

    def test_four_word_run_from_private_text_is_blocked(self):
        goal = "I want to paint a small mural near the library by next spring"
        r = self.gate().check("how do people paint a small mural", private_texts=[goal])
        self.assertFalse(r.ok)
        self.assertTrue(any("copied" in x for x in r.reasons))

    def test_three_word_overlap_is_allowed(self):
        goal = "paint a small mural near the library"
        self.assertTrue(self.gate().check("what is a small mural", private_texts=[goal]).ok)

    def test_run_match_ignores_case_and_punctuation(self):
        goal = "Order the Marlow-kiln parts before June."
        r = self.gate().check("compare order the marlow kiln parts", private_texts=[goal])
        self.assertFalse(r.ok)

    def test_contact_patterns(self):
        for q in ("learn about someone@example.com glazes",
                  "learn about call +1 (303) 555-0134 glazes",
                  "learn 303-555-0134 about glazes"):
            self.assertFalse(self.gate().check(q).ok, q)

    def test_currency_amounts(self):
        for q in ("how to spend $5000 on kiln shelves", "how to spend 5,000 dollars",
                  "how to spend 40k USD", "how to spend €300"):
            self.assertFalse(self.gate().check(q).ok, q)

    def test_digit_runs_other_than_a_year(self):
        for q in ("what is the best way 12345", "kiln shelves 42", "glaze recipes 20260"):
            self.assertFalse(self.gate().check(q).ok, q)
        self.assertFalse(self.gate().check("kiln shelves 3000").ok)  # 4 digits, not a year

    def test_missing_blocked_file_fails_closed(self):
        self.blocked_path.unlink()
        g = self.gate()
        r = g.check("what is the best way to learn about kiln shelves")
        self.assertFalse(r.ok)
        self.assertTrue(any("blocked-names" in x for x in r.reasons))

    def test_unreadable_blocked_file_fails_closed(self):
        self.blocked_path.write_bytes(b"\xff\xfe\x00bad")
        self.assertFalse(self.gate().check("what is the best way").ok)

    def test_empty_blocked_file_fails_closed(self):
        self.blocked_path.write_text("# nothing\n\n", encoding="utf-8")
        self.assertFalse(self.gate().check("what is the best way").ok)

    def test_missing_dictionary_fails_closed(self):
        # Queries are all-lowercase, so without the dictionary the lowercase-name check
        # would silently pass "how does dana negotiate": block everything instead.
        self.dict_path.unlink()
        g = self.gate()
        for q in ("What is the best way", "what is the best way", "how does dana negotiate"):
            r = g.check(q)
            self.assertFalse(r.ok, q)
            self.assertTrue(any("dictionary" in x for x in r.reasons), r.reasons)

    def test_empty_dictionary_fails_closed(self):
        self.dict_path.write_text("\n", encoding="utf-8")
        self.assertFalse(self.gate().check("what is the best way").ok)

    def test_from_config_reads_paths(self):
        cfg = {"frontier": {"egress": {
            "dictionary_path": str(self.dict_path),
            "blocked_names_path": str(self.blocked_path),
            "allowed_proper_nouns": ["Python"], "phrase_run_words": 4,
            "year_min": 1900, "year_max": 2100}}}
        g = EC.Gate.from_config(cfg)
        self.assertFalse(g.check("jane roe").ok)

    def test_real_config_loads(self):
        import dreamer_common as dc
        e = dc.CFG["frontier"]["egress"]
        for k in ("dictionary_path", "blocked_names_path", "allowed_proper_nouns",
                  "phrase_run_words", "year_min", "year_max", "log_path"):
            self.assertIn(k, e)

    def test_normalize_query(self):
        self.assertEqual(EC.normalize_query("  Best   WAY\tto learn "), "best way to learn")

    def test_approved_file_roundtrip(self):
        p = self.tmp / "approved.txt"
        EC.write_approved(p, ["Best  Way", "other query"])
        self.assertEqual(EC.read_approved(p), {"best way", "other query"})


def run_hook(payload, env_extra=None, raw=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DREAMER_EGRESS")}
    env.update(env_extra or {})
    data = raw if raw is not None else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=data, env=env,
                          capture_output=True, text=True, cwd=str(ROOT))


class HookTest(Base):
    def setUp(self):
        super().setUp()
        self.approved = self.tmp / "approved.txt"
        EC.write_approved(self.approved, ["what is the best way to learn"])
        self.log = self.tmp / "egress.jsonl"
        self.env = {"DREAMER_EGRESS_APPROVED": str(self.approved),
                    "DREAMER_EGRESS_LOG": str(self.log)}

    def decision(self, proc):
        out = json.loads(proc.stdout)  # stdout must be exactly one JSON document
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PreToolUse")
        return h["permissionDecision"], h.get("permissionDecisionReason", "")

    def entries(self):
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()]

    def test_approved_query_allowed_and_logged(self):
        p = run_hook({"tool_name": "WebSearch",
                      "tool_input": {"query": "What is the BEST way  to learn", "mode": "standard"}},
                     self.env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.decision(p)[0], "allow")
        self.assertEqual(p.stderr, "")
        e = self.entries()
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["decision"], "allow")
        self.assertEqual(e[0]["tool"], "WebSearch")
        self.assertIn("best way", e[0]["query"].lower())
        self.assertIn("at", e[0])
        self.assertIn("reason", e[0])

    def test_unapproved_query_denied_and_logged(self):
        p = run_hook({"tool_name": "WebSearch", "tool_input": {"query": "Jane Roe address"}}, self.env)
        d, reason = self.decision(p)
        self.assertEqual(d, "deny")
        self.assertIn("not approved", reason)
        e = self.entries()
        self.assertEqual(e[0]["decision"], "deny")
        self.assertEqual(e[0]["query"], "Jane Roe address")

    def test_webfetch_and_other_tools_denied_and_logged(self):
        for tool, inp in (("WebFetch", {"url": "https://example.com", "prompt": "x"}),
                          ("Bash", {"command": "ls"}), ("Read", {"file_path": "/etc/passwd"}),
                          ("mcp__studio__log_firing", {})):
            p = run_hook({"tool_name": tool, "tool_input": inp}, self.env)
            self.assertEqual(self.decision(p)[0], "deny", tool)
        e = self.entries()
        self.assertEqual([x["tool"] for x in e],
                         ["WebFetch", "Bash", "Read", "mcp__studio__log_firing"])
        self.assertTrue(all(x["decision"] == "deny" for x in e))
        self.assertIn("example.com", e[0]["input"])

    def test_bad_json_denied(self):
        p = run_hook(None, self.env, raw="{not json")
        self.assertEqual(self.decision(p)[0], "deny")
        self.assertEqual(self.entries()[0]["decision"], "deny")

    def test_non_object_payload_denied(self):
        self.assertEqual(self.decision(run_hook([1, 2], self.env))[0], "deny")

    def test_missing_query_denied(self):
        p = run_hook({"tool_name": "WebSearch", "tool_input": {}}, self.env)
        self.assertEqual(self.decision(p)[0], "deny")

    def test_missing_env_denied(self):
        p = run_hook({"tool_name": "WebSearch", "tool_input": {"query": "x"}}, {})
        self.assertEqual(self.decision(p)[0], "deny")
        p = run_hook({"tool_name": "WebSearch", "tool_input": {"query": "x"}},
                     {"DREAMER_EGRESS_LOG": str(self.log)})
        self.assertEqual(self.decision(p)[0], "deny")
        self.assertIn("approved", self.entries()[-1]["reason"])

    def test_missing_approved_file_denied(self):
        self.approved.unlink()
        p = run_hook({"tool_name": "WebSearch",
                      "tool_input": {"query": "what is the best way to learn"}}, self.env)
        self.assertEqual(self.decision(p)[0], "deny")

    def test_unwritable_log_denies_even_an_approved_query(self):
        env = dict(self.env, DREAMER_EGRESS_LOG=str(self.tmp / "no" / "such" / "dir" / "e.jsonl"))
        p = run_hook({"tool_name": "WebSearch",
                      "tool_input": {"query": "what is the best way to learn"}}, env)
        self.assertEqual(self.decision(p)[0], "deny")

    def test_raising_decider_prints_explicit_deny(self):
        import egress_hook as EH
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(EH, "decide", side_effect=RuntimeError("boom")), \
             mock.patch("sys.stdin", io.StringIO('{"tool_name":"WebSearch","tool_input":{"query":"x"}}')), \
             mock.patch("sys.stdout", out):
            rc = EH.main()
        self.assertEqual(rc, 0)
        h = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(h["permissionDecision"], "deny")
        self.assertIn("boom", h["permissionDecisionReason"])
        self.assertEqual(self.entries()[0]["decision"], "deny")


RESULTS_HOOK = ROOT / "scripts" / "egress_results_hook.py"


def run_results_hook(payload, env_extra=None, raw=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DREAMER_EGRESS")}
    env.update(env_extra or {})
    data = raw if raw is not None else json.dumps(payload)
    return subprocess.run([sys.executable, str(RESULTS_HOOK)], input=data, env=env,
                          capture_output=True, text=True, cwd=str(ROOT))


class NormalizeUrlTest(unittest.TestCase):
    def test_scheme_host_lowercased_fragment_and_trailing_slash_dropped(self):
        n = EC.normalize_url
        self.assertEqual(n("HTTPS://Example.COM/Path/"), "https://example.com/Path")
        self.assertEqual(n("https://example.com/a#frag"), n("https://example.com/a/"))
        self.assertEqual(n("https://example.com/a?x=1#f"), "https://example.com/a?x=1")
        self.assertNotEqual(n("https://example.com/A"), n("https://example.com/a"))

    def test_non_http_or_hostless_is_empty(self):
        for u in ("ftp://example.com/x", "javascript:alert(1)", "https:///x", "not a url", "https://[x"):
            self.assertEqual(EC.normalize_url(u), "", u)


class ResultsHookTest(Base):
    def setUp(self):
        super().setUp()
        self.results = self.tmp / "results.txt"
        self.log = self.tmp / "egress.jsonl"
        self.env = {"DREAMER_EGRESS_RESULTS": str(self.results),
                    "DREAMER_EGRESS_LOG": str(self.log)}

    def recorded(self):
        return set(self.results.read_text(encoding="utf-8").split()) if self.results.exists() else set()

    def test_records_every_url_anywhere_in_the_response(self):
        payload = {"tool_name": "WebSearch", "tool_input": {"query": "q"},
                   "tool_response": {"query": "q", "results": [
                       {"title": "A", "url": "https://Example.com/a/"},
                       {"content": [{"title": "B", "url": "http://other.org/b#x"}]}],
                       "text": "see [C](https://c.example/path) and https://d.example/p."}}
        p = run_results_hook(payload, self.env)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        # trailing prose punctuation is kept as an extra reading, so assert containment
        self.assertTrue({"https://example.com/a", "http://other.org/b", "https://c.example/path",
                         "https://d.example/p"} <= self.recorded())

    def test_string_response_and_no_urls(self):
        run_results_hook({"tool_name": "WebSearch", "tool_response": "Links: https://s.example/x"}, self.env)
        self.assertEqual(self.recorded(), {"https://s.example/x"})
        self.results.unlink()
        run_results_hook({"tool_name": "WebSearch", "tool_response": {"results": []}}, self.env)
        self.assertEqual(self.recorded(), set())

    def test_other_tools_record_nothing(self):
        run_results_hook({"tool_name": "WebFetch", "tool_response": "https://x.example/"}, self.env)
        self.assertEqual(self.recorded(), set())

    def test_failures_log_and_record_nothing(self):
        for kwargs in ({"raw": "{not json"}, {"payload": [1]}):
            p = run_results_hook(kwargs.get("payload"), self.env, raw=kwargs.get("raw"))
            self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertEqual(self.recorded(), set())
        # a non-object payload is "no URLs", bad JSON is a logged failure
        entries = [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["decision"] for e in entries], ["record_failed"])

    def test_missing_env_records_nothing(self):
        p = run_results_hook({"tool_name": "WebSearch", "tool_response": "https://x.example/"},
                             {"DREAMER_EGRESS_LOG": str(self.log)})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.recorded(), set())
        self.assertIn("DREAMER_EGRESS_RESULTS", self.log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
