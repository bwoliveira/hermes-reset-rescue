"""T-09/T-71: the DTO allow-list serializer.

Every DTO must emit exactly its listed fields; an extra attribute must raise;
no field may be named ``profile`` or look like a credential. Quota is allowed
only as the compact summary D-017 restored: a plan name plus each present
window's *remaining* percent (``QuotaDTO``), never a used percent and never
anywhere else.
"""

from __future__ import annotations

import json
import unittest

from reset_rescue.dto import (
    CREDIT_STATES,
    DTO,
    LIMIT_SUMMARY_FIELDS,
    OUTCOMES,
    AccountDTO,
    AttentionDTO,
    CreditDTO,
    DTOLeak,
    EnsureDTO,
    HistoryDTO,
    HistoryItemDTO,
    JobDTO,
    LastResultDTO,
    NoticeResultDTO,
    QuotaCheckDTO,
    QuotaDTO,
    RedeemResultDTO,
    ScheduleDTO,
    SettingsViewDTO,
    SlotDTO,
    StatusDTO,
    WrapperStateDTO,
    iso_z,
    to_json,
)

ALL_DTOS = (
    AccountDTO,
    AttentionDTO,
    CreditDTO,
    EnsureDTO,
    HistoryDTO,
    HistoryItemDTO,
    JobDTO,
    LastResultDTO,
    NoticeResultDTO,
    QuotaCheckDTO,
    QuotaDTO,
    RedeemResultDTO,
    ScheduleDTO,
    SettingsViewDTO,
    SlotDTO,
    StatusDTO,
    WrapperStateDTO,
)


class AllowListBasics(unittest.TestCase):
    def test_every_dto_declares_a_non_empty_field_tuple(self):
        for cls in ALL_DTOS:
            self.assertTrue(cls.FIELDS, f"{cls.__name__} has no FIELDS")
            self.assertEqual(
                len(cls.FIELDS), len(set(cls.FIELDS)), f"{cls.__name__} declares a duplicate field"
            )

    def test_every_field_is_a_declared_annotated_attribute(self):
        """``FIELDS`` and the class annotations must be the same set.

        ``_Record`` assigns through ``setattr``, so nothing in the class body
        told a type checker the attribute exists: mypy read
        ``account.account_key``, ``credit.expires_at`` and ``slot.fire_at`` as
        attribute errors even though the values were there at runtime. The
        annotations are the type contract now, and this test keeps them from
        drifting away from ``FIELDS`` in either direction — a field added to
        one and not the other fails here instead of in a consumer's mypy run.
        """
        for cls in ALL_DTOS:
            annotations = set(getattr(cls, "__annotations__", {}))
            self.assertEqual(
                annotations,
                set(cls.FIELDS),
                f"{cls.__name__}: annotations {sorted(annotations)} != FIELDS {sorted(cls.FIELDS)}",
            )

    def test_the_annotated_types_match_what_the_engine_builds(self):
        """The three fields a consumer reads with the wrong name or type.

        These are the exact mypy findings on ``service.py``: the DTOs had no
        declared ``account_key`` / ``expires_at`` / ``fire_at``, so a consumer
        could only reach them through ``Any``. A wrong name is a ``DTOLeak``
        at construction and an ``AttributeError`` on ``to_dict``; a wrong type
        is caught by the annotation check above.
        """
        account = AccountDTO(
            account_key="a" * 16,
            label="a***@e***.com",
            included=True,
            reachable=True,
            last_error_code=None,
            last_checked_at=None,
            quota=None,
            credits=[],
        )
        self.assertEqual(account.account_key, "a" * 16)

        credit = CreditDTO(
            credit_key="c" * 16,
            title="t",
            reset_type="r",
            granted_at=None,
            expires_at="2026-11-09T21:30:00Z",
            state="available",
            plan="scheduled",
            primary=None,
            backup=None,
            last_result=None,
            can_redeem_now=True,
        )
        self.assertEqual(credit.expires_at, "2026-11-09T21:30:00Z")

        slot = SlotDTO(role="primary", fire_at="2026-11-09T21:20:00Z", job_state="armed")
        self.assertEqual(slot.fire_at, "2026-11-09T21:20:00Z")

        with self.assertRaises(DTOLeak):
            AccountDTO(account_key="a" * 16, accountKey="typo")
        with self.assertRaises(DTOLeak):
            CreditDTO(credit_key="c" * 16, expiresAt="typo")
        with self.assertRaises(DTOLeak):
            SlotDTO(role="primary", fireAt="typo")

    def test_constructor_rejects_an_unknown_keyword(self):
        with self.assertRaises(DTOLeak):
            JobDTO(
                name="n",
                job_id="j",
                schedule_display="every 12h",
                enabled=True,
                access_token="nope",
            )

    def test_missing_keywords_become_none(self):
        dto = JobDTO(name="n")
        self.assertIsNone(dto.job_id)
        self.assertEqual(dto.to_dict()["name"], "n")

    def test_extra_attribute_raises_on_serialize(self):
        dto = JobDTO(name="n", job_id="j", schedule_display="s", enabled=True)
        dto.raw_body = {"credits": []}
        with self.assertRaises(DTOLeak) as ctx:
            dto.to_dict()
        self.assertIn("raw_body", str(ctx.exception))

    def test_extra_attribute_raises_on_repr_too(self):
        dto = JobDTO(name="n", job_id="j", schedule_display="s", enabled=True)
        dto.secret_token = "abc"
        with self.assertRaises(DTOLeak):
            repr(dto)

    def test_underscore_attributes_are_private_and_allowed(self):
        dto = JobDTO(name="n", job_id="j", schedule_display="s", enabled=True)
        dto._cache = {"anything": object()}
        self.assertEqual(
            dto.to_dict(), {"name": "n", "job_id": "j", "schedule_display": "s", "enabled": True}
        )

    def test_check_returns_self_for_chaining(self):
        dto = JobDTO(name="n", job_id="j", schedule_display="s", enabled=True)
        self.assertIs(dto.check(), dto)

    def test_to_json_round_trips(self):
        dto = JobDTO(name="n", job_id="j", schedule_display="every 12h", enabled=True)
        self.assertEqual(
            json.loads(dto.to_json()),
            {"name": "n", "job_id": "j", "schedule_display": "every 12h", "enabled": True},
        )


class NoProfileField(unittest.TestCase):
    def test_no_dto_declares_a_profile_field(self):
        for cls in ALL_DTOS:
            for name in cls.FIELDS:
                self.assertNotEqual(name, "profile", f"{cls.__name__}.{name}")
                self.assertNotIn("profile", name.lower(), f"{cls.__name__}.{name}")

    def test_profile_keyword_is_rejected(self):
        with self.assertRaises(DTOLeak):
            JobDTO(name="n", job_id="j", schedule_display="s", enabled=True, profile="default")


class NoCredentialLookingField(unittest.TestCase):
    FORBIDDEN = (
        "token",
        "secret",
        "authorization",
        "cookie",
        "password",
        "api_key",
        "base_url",
        "refresh",
    )

    def test_no_field_name_looks_like_a_credential(self):
        for cls in ALL_DTOS:
            for name in cls.FIELDS:
                lowered = name.lower()
                for part in self.FORBIDDEN:
                    self.assertNotIn(part, lowered, f"{cls.__name__}.{name} contains {part}")


class QuotaSummaryIsTheOnlyQuotaField(unittest.TestCase):
    """D-017: the compact summary is plan type plus remaining percent.

    D-010's "no quota percentages" still holds everywhere else: the allow-list
    names two exceptions — ``AccountDTO.quota`` and the two remaining-percent
    fields of ``QuotaDTO`` — and nothing else may grow a percentage, a used
    figure or a per-credit quota field.
    """

    FORBIDDEN = ("pct", "percent", "quota", "usage", "weekly", "five_hour", "used")

    def test_only_the_limit_window_dto_may_declare_a_percentage(self):
        for cls in ALL_DTOS:
            allowed = LIMIT_SUMMARY_FIELDS.get(cls.__name__, ())
            for name in cls.FIELDS:
                if name in allowed:
                    continue
                lowered = name.lower()
                for part in self.FORBIDDEN:
                    self.assertNotIn(part, lowered, f"{cls.__name__}.{name} contains {part}")

    def test_the_only_allowed_percentage_fields_are_remaining(self):
        self.assertEqual(
            LIMIT_SUMMARY_FIELDS,
            {
                "AccountDTO": ("quota",),
                "QuotaDTO": ("five_hour_remaining_pct", "weekly_remaining_pct"),
                "SettingsViewDTO": ("early_redeem_remaining_pct",),
            },
        )
        self.assertEqual(
            QuotaDTO.FIELDS, ("plan_type", "five_hour_remaining_pct", "weekly_remaining_pct")
        )

    def test_there_is_no_used_percent_field(self):
        for name in QuotaDTO.FIELDS:
            self.assertNotIn("used", name.lower())
        with self.assertRaises(DTOLeak):
            QuotaDTO(plan_type="Pro", used_pct=42)

    def test_a_usage_shaped_field_is_rejected_at_construction(self):
        for name in ("weekly_pct", "five_hour_pct", "quota_percent", "usage"):
            with self.assertRaises(DTOLeak):
                JobDTO(
                    **{
                        "name": "n",
                        "job_id": "j",
                        "schedule_display": "s",
                        "enabled": True,
                        name: 42,
                    }
                )

    def test_a_redeem_result_has_no_usage_field(self):
        self.assertNotIn("usage", RedeemResultDTO.FIELDS)
        self.assertEqual(
            RedeemResultDTO.FIELDS,
            ("credit_key", "outcome", "reason_code", "windows_reset"),
        )

    def test_a_credit_has_no_quota_field(self):
        for name in CreditDTO.FIELDS:
            self.assertNotIn("pct", name.lower())
            self.assertNotIn("quota", name.lower())

    def test_the_account_carries_the_summary_as_one_nested_object(self):
        self.assertIn("quota", AccountDTO.FIELDS)
        self.assertEqual(
            QuotaDTO.FIELDS, ("plan_type", "five_hour_remaining_pct", "weekly_remaining_pct")
        )

    def test_there_is_no_usage_dto_at_all(self):
        import reset_rescue.dto as dto_module

        self.assertFalse(hasattr(dto_module, "UsageDTO"))


class FieldNamesAreValidated(unittest.TestCase):
    """A subclass that extends FIELDS cannot smuggle a forbidden name in."""

    def test_a_subclass_adding_a_token_field_is_rejected(self):
        class Leaky(JobDTO):
            FIELDS = JobDTO.FIELDS + ("access_token",)

        with self.assertRaises(DTOLeak) as ctx:
            Leaky(
                name="n", job_id="j", schedule_display="s", enabled=True, access_token="secret"
            ).to_dict()
        self.assertIn("access_token", str(ctx.exception))

    def test_a_subclass_adding_a_quota_field_is_rejected(self):
        class Leaky(JobDTO):
            FIELDS = JobDTO.FIELDS + ("weekly_pct",)

        with self.assertRaises(DTOLeak):
            Leaky(
                name="n", job_id="j", schedule_display="s", enabled=True, weekly_pct=42.0
            ).to_dict()

    def test_a_subclass_of_the_summary_may_not_add_used_pct(self):
        class Leaky(QuotaDTO):
            FIELDS = QuotaDTO.FIELDS + ("used_pct",)

        with self.assertRaises(DTOLeak):
            Leaky(
                plan_type="Pro", five_hour_remaining_pct=None, weekly_remaining_pct=10, used_pct=90
            ).to_dict()

    def test_a_subclass_adding_a_profile_field_is_rejected(self):
        class Leaky(JobDTO):
            FIELDS = JobDTO.FIELDS + ("profile",)

        with self.assertRaises(DTOLeak):
            Leaky(
                name="n", job_id="j", schedule_display="s", enabled=True, profile="default"
            ).to_dict()

    def test_a_harmless_subclass_is_allowed(self):
        class Extended(JobDTO):
            FIELDS = JobDTO.FIELDS + ("note",)

        self.assertEqual(
            Extended(name="n", job_id="j", schedule_display="s", enabled=True, note="hi").to_dict()[
                "note"
            ],
            "hi",
        )


class NestedSerialization(unittest.TestCase):
    def test_nested_dtos_serialize_recursively(self):
        credit = CreditDTO(
            credit_key="a" * 16,
            title="Full reset",
            reset_type="codex_rate_limits",
            granted_at=iso_z(1_800_000_000),
            expires_at=iso_z(1_800_003_600),
            state="available",
            plan="scheduled",
            primary=SlotDTO(role="primary", fire_at=iso_z(1_800_003_000), job_state="armed"),
            backup=None,
            last_result=LastResultDTO(code="ok", at=iso_z(1_800_000_000), reason_code=None),
            can_redeem_now=True,
        )
        payload = credit.to_dict()
        self.assertEqual(payload["primary"]["role"], "primary")
        self.assertEqual(payload["backup"], None)
        self.assertEqual(payload["last_result"]["code"], "ok")

    def test_list_of_dtos_serializes(self):
        status = StatusDTO(
            version="0.1.0",
            schema_version=1,
            source_kind="pool",
            coverage_note_key="coverage.pool",
            dry_run=False,
            language="en",
            settings=SettingsViewDTO(
                scan_interval_hours=12,
                lead_minutes=10,
                backup_lead_minutes=4,
                notify_target_display="local",
                excluded_accounts_count=0,
                mask_labels=True,
                early_redeem_enabled=False,
                early_redeem_remaining_pct=0,
            ),
            settings_warnings=[],
            schedule=None,
            accounts=[],
            attention=None,
        )
        payload = status.to_dict()
        self.assertEqual(payload["settings"]["scan_interval_hours"], 12)
        self.assertEqual(payload["accounts"], [])

    def test_a_nested_extra_attribute_still_raises(self):
        slot = SlotDTO(role="primary", fire_at=None, job_state="armed")
        slot.raw_job = {"id": "x"}
        credit = CreditDTO(
            credit_key="a" * 16,
            title="t",
            reset_type="r",
            granted_at=None,
            expires_at=None,
            state="available",
            plan="none",
            primary=slot,
            backup=None,
            last_result=None,
            can_redeem_now=False,
        )
        with self.assertRaises(DTOLeak):
            credit.to_dict()

    def test_to_json_handles_a_plain_structure_of_dtos(self):
        text = to_json(
            {"items": [JobDTO(name="n", job_id="j", schedule_display="s", enabled=True)]}
        )
        self.assertEqual(
            json.loads(text),
            {"items": [{"name": "n", "job_id": "j", "schedule_display": "s", "enabled": True}]},
        )

    def test_an_arbitrary_object_is_refused(self):
        with self.assertRaises(DTOLeak):
            to_json({"bad": object()})


class TimeRendering(unittest.TestCase):
    def test_iso_z_is_utc_with_a_z_suffix(self):
        self.assertEqual(iso_z(0), "1970-01-01T00:00:00Z")
        self.assertEqual(iso_z(None), None)

    def test_iso_z_is_second_precision(self):
        self.assertEqual(iso_z(1_800_000_000.987), "2027-01-15T08:00:00Z")


class EnumSets(unittest.TestCase):
    def test_credit_states_match_the_architecture(self):
        self.assertEqual(
            CREDIT_STATES,
            {"available", "pending", "redeemed", "vanished", "expired", "unknown"},
        )

    def test_outcomes_match_the_architecture(self):
        self.assertEqual(
            OUTCOMES,
            {
                "redeemed",
                "already_redeemed",
                "nothing_to_reset",
                "vanished",
                "failed",
                "unconfirmed",
                "dry_run",
            },
        )


class FullStatusShape(unittest.TestCase):
    def test_a_complete_status_dto_serializes_to_the_documented_keys(self):
        status = StatusDTO(
            version="0.1.0",
            schema_version=1,
            source_kind="pool",
            coverage_note_key="coverage.pool",
            dry_run=True,
            auto_redeem_enabled=False,
            language="pt-BR",
            settings=SettingsViewDTO(
                scan_interval_hours=12,
                lead_minutes=10,
                backup_lead_minutes=4,
                notify_target_display="telegram:…4588:…131",
                excluded_accounts_count=2,
                mask_labels=False,
                auto_redeem_enabled=False,
                early_redeem_enabled=True,
                early_redeem_remaining_pct=10,
            ),
            settings_warnings=["settings_lead_invalid"],
            schedule=ScheduleDTO(
                armed=True,
                scan_job=JobDTO(
                    name="reset-rescue-scan",
                    job_id="job-1",
                    schedule_display="every 12h",
                    enabled=True,
                ),
                early_job=None,
                next_scan_at=iso_z(1_800_000_000),
                last_scan_at=None,
                last_scan_outcome="ok",
                wrappers=[WrapperStateDTO(name="reset_rescue_scan.py", state="ok")],
                problem_code=None,
            ),
            accounts=[
                AccountDTO(
                    account_key="b" * 16,
                    label="b***@e***.com",
                    included=True,
                    reachable=True,
                    last_error_code=None,
                    last_checked_at=iso_z(1_800_000_000),
                    quota=QuotaDTO(
                        plan_type="Pro 200", five_hour_remaining_pct=88, weekly_remaining_pct=41
                    ),
                    credits=[],
                )
            ],
            attention=AttentionDTO(failed=0, pending=1, next_fire_at=iso_z(1_800_003_000)),
        )
        payload = status.to_dict()
        self.assertEqual(
            set(payload),
            {
                "version",
                "schema_version",
                "source_kind",
                "coverage_note_key",
                "dry_run",
                "auto_redeem_enabled",
                "language",
                "settings",
                "settings_warnings",
                "schedule",
                "accounts",
                "attention",
                "account_count",
                "pool_account_count",  # D-030
                "per_account_available",  # D-030
            },
        )
        self.assertEqual(payload["schedule"]["wrappers"][0]["state"], "ok")
        self.assertEqual(payload["settings"]["notify_target_display"], "telegram:…4588:…131")
        quota = payload["accounts"][0]["quota"]
        self.assertEqual(quota["plan_type"], "Pro 200")
        self.assertEqual(quota["five_hour_remaining_pct"], 88)
        self.assertEqual(quota["weekly_remaining_pct"], 41)

    def test_the_summary_carries_no_used_percentage(self):
        payload = QuotaDTO(
            plan_type="Pro", five_hour_remaining_pct=None, weekly_remaining_pct=41
        ).to_dict()
        self.assertEqual(
            payload,
            {"plan_type": "Pro", "five_hour_remaining_pct": None, "weekly_remaining_pct": 41},
        )
        self.assertNotIn("used", json.dumps(payload))

    def test_a_quota_check_dto_serializes_counts_not_percentages(self):
        dto = QuotaCheckDTO(
            exit_code=0,
            problem_code="",
            accounts=2,
            cooled=1,
            redeemed=1,
            failed=0,
            results=[
                RedeemResultDTO(
                    credit_key="a" * 16, outcome="redeemed", reason_code="reset", windows_reset=1
                )
            ],
        )
        payload = dto.to_dict()
        self.assertEqual(payload["accounts"], 2)
        self.assertEqual(payload["results"][0]["outcome"], "redeemed")
        self.assertNotIn("pct", json.dumps(payload))

    def test_dto_base_class_has_no_fields_and_emits_nothing(self):
        self.assertEqual(DTO().to_dict(), {})


if __name__ == "__main__":
    unittest.main()
