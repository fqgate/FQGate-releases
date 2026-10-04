import copy
import unittest

from v2_acceptance import TARGETS, validate_acceptance


CANDIDATE_SHA256 = "ab" * 32
INTENT_SHA256 = "cd" * 32
VERSION = "2.0.1"
SEQUENCE = 17
REFRESH_SEQUENCE = 23
STATE_IDS = ("previous-1", "previous-2")


def report(state_ids=STATE_IDS):
    return {
        "schemaVersion": 1,
        "version": VERSION,
        "candidateSha256": CANDIDATE_SHA256,
        "intentSha256": INTENT_SHA256,
        "sequence": SEQUENCE,
        "refreshSequence": REFRESH_SEQUENCE,
        "cases": [
            {
                "platform": platform,
                "architecture": architecture,
                "stateId": state_id,
                "processRunning": True,
                "coreRunning": True,
                "updateCheckCompleted": True,
                "automaticExit": False,
                "securityStatePreserved": True,
                "evidenceSha256": "ef" * 32,
            }
            for platform, architecture in sorted(TARGETS)
            for state_id in ("fresh", *state_ids)
        ],
    }


def check(value, state_ids=STATE_IDS, **overrides):
    params = {
        "candidate_sha256": CANDIDATE_SHA256,
        "version": VERSION,
        "intent_sha256": INTENT_SHA256,
        "sequence": SEQUENCE,
        "refresh_sequence": REFRESH_SEQUENCE,
        "state_ids": state_ids,
    }
    params.update(overrides)
    return validate_acceptance(value, **params)


class AcceptanceTests(unittest.TestCase):
    def test_complete_bound_report_is_returned_without_mutation(self):
        value = report()
        original = copy.deepcopy(value)
        self.assertIs(check(value), value)
        self.assertEqual(value, original)
        self.assertEqual(len(value["cases"]), 9)

    def test_empty_history_still_requires_all_three_fresh_targets(self):
        value = report(())
        self.assertIs(check(value, ()), value)
        self.assertEqual(len(value["cases"]), 3)

    def test_case_order_is_not_evidence_and_does_not_change_coverage(self):
        value = report()
        value["cases"].reverse()
        self.assertIs(check(value), value)

    def test_root_and_case_fields_must_be_exact(self):
        for scope in ("report", "case"):
            base = report() if scope == "report" else report()["cases"][0]
            for field in base:
                value = report()
                target = value if scope == "report" else value["cases"][0]
                del target[field]
                with self.subTest(scope=scope, missing=field), self.assertRaises(
                    ValueError
                ):
                    check(value)
            value = report()
            target = value if scope == "report" else value["cases"][0]
            target["unapproved"] = True
            with self.subTest(scope=scope, extra=True), self.assertRaises(ValueError):
                check(value)

    def test_every_publication_binding_must_match(self):
        replacements = {
            "version": "2.0.2",
            "candidateSha256": "00" * 32,
            "intentSha256": "11" * 32,
            "sequence": SEQUENCE + 1,
            "refreshSequence": REFRESH_SEQUENCE + 1,
        }
        for field, replacement in replacements.items():
            value = report()
            value[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "不一致"
            ):
                check(value)

    def test_integers_are_not_booleans_floats_strings_or_zero(self):
        for field in ("schemaVersion", "sequence", "refreshSequence"):
            for replacement in (True, False, 1.0, "1", 0, -1, None):
                value = report()
                value[field] = replacement
                with self.subTest(field=field, value=replacement), self.assertRaises(
                    ValueError
                ):
                    check(value)

    def test_missing_extra_and_duplicate_cases_are_rejected(self):
        missing = report()
        missing["cases"].pop()
        extra = report()
        extra["cases"].append(copy.deepcopy(extra["cases"][0]))
        duplicate = report()
        duplicate["cases"][-1] = copy.deepcopy(duplicate["cases"][0])
        for label, value in (
            ("missing", missing),
            ("extra", extra),
            ("duplicate", duplicate),
        ):
            with self.subTest(label=label), self.assertRaises(ValueError):
                check(value)

    def test_unapproved_target_or_security_state_is_rejected(self):
        for field, replacement in (
            ("platform", "linux"),
            ("architecture", "arm64"),
            ("stateId", "previous-unapproved"),
            ("stateId", ""),
            ("stateId", ["fresh"]),
        ):
            value = report()
            value["cases"][0][field] = replacement
            with self.subTest(field=field, value=replacement), self.assertRaises(
                ValueError
            ):
                check(value)

    def test_all_success_flags_require_literal_true(self):
        for field in (
            "processRunning",
            "coreRunning",
            "updateCheckCompleted",
            "securityStatePreserved",
        ):
            for replacement in (False, 1, "true", None):
                value = report()
                value["cases"][0][field] = replacement
                with self.subTest(field=field, value=replacement), self.assertRaises(
                    ValueError
                ):
                    check(value)

    def test_automatic_exit_requires_literal_false(self):
        for replacement in (True, 0, "false", None):
            value = report()
            value["cases"][0]["automaticExit"] = replacement
            with self.subTest(value=replacement), self.assertRaises(ValueError):
                check(value)

    def test_every_digest_is_full_hexadecimal_sha256(self):
        for field in ("candidateSha256", "intentSha256", "evidenceSha256"):
            for replacement in (
                "",
                "a" * 63,
                "a" * 65,
                "z" * 64,
                "a" * 64 + "\n",
                1,
                None,
            ):
                value = report()
                target = value["cases"][0] if field == "evidenceSha256" else value
                target[field] = replacement
                with self.subTest(field=field, value=replacement), self.assertRaises(
                    ValueError
                ):
                    check(value)
        value = report()
        value["cases"][0]["evidenceSha256"] = "AF" * 32
        self.assertIs(check(value), value)

    def test_report_and_cases_container_types_are_strict(self):
        for value in (None, [], "report", True):
            with self.subTest(report=value), self.assertRaises(ValueError):
                check(value)
        for replacement in (None, {}, (), "cases"):
            value = report()
            value["cases"] = replacement
            with self.subTest(cases=replacement), self.assertRaises(ValueError):
                check(value)
        value = report()
        value["cases"][0] = None
        with self.assertRaises(ValueError):
            check(value)

    def test_expected_states_cannot_silently_change_the_matrix(self):
        for state_ids in (
            None,
            "previous-1",
            {"previous-1": True},
            ("fresh",),
            ("duplicate", "duplicate"),
            ("",),
            (" padded",),
            ("control\n",),
            (1,),
        ):
            with self.subTest(state_ids=state_ids), self.assertRaises(ValueError):
                check(report(), state_ids)

    def test_expected_publication_identity_must_itself_be_typed_and_valid(self):
        for params in (
            {"candidate_sha256": "not-a-digest"},
            {"intent_sha256": "not-a-digest"},
            {"version": None},
            {"version": ""},
            {"sequence": True},
            {"sequence": 0},
            {"refresh_sequence": False},
            {"refresh_sequence": 0},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                check(report(), **params)


if __name__ == "__main__":
    unittest.main()
