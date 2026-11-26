import uuid
from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ObjectDoesNotExist
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext, override_settings

from core.models import target_queryset
from individual.models import IndividualDataSourceUpload
from social_protection.models import (
    Beneficiary,
    BenefitPlan,
    BenefitPlanDataUploadRecords,
    GroupBeneficiary,
    Programme,
    Project,
    UserProgramme,
)
from social_protection.services import BeneficiaryService, GroupBeneficiaryService
from social_protection.programme_scope import (
    INVALID_HEADER_MESSAGE,
    OUT_OF_SCOPE_MESSAGE,
    ProgrammeScopeError,
    assert_benefit_plan_in_scope,
    get_programme_scope,
    scope_benefit_plans,
    scope_queryset,
)
from social_protection.tests.programme_helpers import (
    add_member,
    create_programme,
    create_programme_benefit_plan,
    create_user_with_rights,
    get_admin_user,
)
from social_protection.tests.test_helpers import (
    add_group_to_benefit_plan,
    add_individual_to_benefit_plan,
    create_group_with_individual,
    create_individual,
    create_project,
)


def _request(programme_id=None):
    headers = {} if programme_id is None else {"HTTP_X_PROGRAMME": str(programme_id)}
    return RequestFactory().get("/", **headers)


def _membership_queries(captured):
    return [q for q in captured.captured_queries if "social_protection_userprogramme" in q["sql"]]


class ProgrammeScopeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        username = cls.admin.username
        cls.programme_a = create_programme("PSA", username)
        cls.programme_b = create_programme("PSB", username)
        cls.programme_deleted = create_programme("PSDEL", username, is_deleted=True)

        cls.member_a = create_user_with_rights("psMemberA")
        cls.member_b = create_user_with_rights("psMemberB")
        cls.member_ab = create_user_with_rights("psMemberAB")
        cls.no_membership = create_user_with_rights("psNoMember")
        cls.removed_member = create_user_with_rights("psRemoved")
        add_member(cls.member_a, cls.programme_a, username)
        add_member(cls.member_a, cls.programme_deleted, username)
        add_member(cls.member_b, cls.programme_b, username)
        add_member(cls.member_ab, cls.programme_a, username)
        add_member(cls.member_ab, cls.programme_b, username)
        add_member(cls.removed_member, cls.programme_a, username, is_deleted=True)

        cls.plan_a = create_programme_benefit_plan("PSPA", cls.programme_a, username)
        cls.plan_b = create_programme_benefit_plan("PSPB", cls.programme_b, username)
        cls.plan_none = create_programme_benefit_plan("PSPN", None, username)
        cls.plan_deleted_programme = create_programme_benefit_plan(
            "PSPD", cls.programme_deleted, username, is_deleted=True
        )
        cls.project_a = create_project("PS project A", cls.plan_a, username)
        cls.project_b = create_project("PS project B", cls.plan_b, username)

    def _visible(self, user, request=None):
        own_plans = BenefitPlan.objects.filter(code__in=("PSPA", "PSPB", "PSPN", "PSPD"))
        return set(scope_benefit_plans(own_plans, user, request).values_list("code", flat=True))

    def test_superuser_sees_every_benefit_plan(self):
        self.assertEqual(self._visible(self.admin), {"PSPA", "PSPB", "PSPN", "PSPD"})

    def test_member_sees_own_programme_and_plans_without_programme(self):
        self.assertEqual(self._visible(self.member_a), {"PSPA", "PSPN"})
        self.assertEqual(self._visible(self.member_b), {"PSPB", "PSPN"})
        self.assertEqual(self._visible(self.member_ab), {"PSPA", "PSPB", "PSPN"})

    def test_user_without_membership_sees_only_plans_without_programme(self):
        self.assertEqual(self._visible(self.no_membership), {"PSPN"})

    def test_deleted_membership_and_deleted_programme_grant_nothing(self):
        self.assertEqual(self._visible(self.removed_member), {"PSPN"})
        self.assertNotIn("PSPD", self._visible(self.member_a))

    def test_anonymous_user_sees_nothing(self):
        self.assertEqual(self._visible(AnonymousUser()), set())

    def test_header_narrows_member_to_the_active_programme(self):
        self.assertEqual(self._visible(self.member_ab, _request(self.programme_a.id)), {"PSPA"})
        self.assertEqual(self._visible(self.member_ab, _request(self.programme_b.id)), {"PSPB"})

    def test_header_narrows_superuser_to_the_active_programme(self):
        self.assertEqual(self._visible(self.admin, _request(self.programme_b.id)), {"PSPB"})

    def test_header_of_a_programme_the_user_does_not_belong_to_is_refused(self):
        with self.assertRaisesMessage(ProgrammeScopeError, INVALID_HEADER_MESSAGE):
            self._visible(self.member_a, _request(self.programme_b.id))

    def test_unknown_malformed_or_deleted_programme_header_is_refused(self):
        for user in (self.member_a, self.admin):
            for value in (uuid.uuid4(), "not-a-uuid", self.programme_deleted.id):
                with self.subTest(user=user.username, value=value):
                    with self.assertRaisesMessage(ProgrammeScopeError, INVALID_HEADER_MESSAGE):
                        self._visible(user, _request(value))

    def test_header_of_a_removed_membership_is_refused(self):
        with self.assertRaisesMessage(ProgrammeScopeError, INVALID_HEADER_MESSAGE):
            self._visible(self.removed_member, _request(self.programme_a.id))

    def test_header_on_anonymous_request_is_refused(self):
        with self.assertRaises(ProgrammeScopeError):
            self._visible(AnonymousUser(), _request(self.programme_a.id))

    def test_empty_header_counts_as_absent(self):
        request = RequestFactory().get("/", HTTP_X_PROGRAMME="  ")
        self.assertEqual(self._visible(self.member_a, request), {"PSPA", "PSPN"})

    def test_scope_queryset_follows_a_benefit_plan_path(self):
        projects = scope_queryset(Project.objects.all(), self.member_a)
        self.assertIn(self.project_a.id, set(projects.values_list("id", flat=True)))
        self.assertNotIn(self.project_b.id, set(projects.values_list("id", flat=True)))

    def test_membership_is_resolved_once_per_request(self):
        request = _request(self.programme_a.id)
        with CaptureQueriesContext(connection) as captured:
            get_programme_scope(self.member_ab, request)
            get_programme_scope(self.member_ab, request)
            scope_benefit_plans(BenefitPlan.objects.all(), self.member_ab, request)
        self.assertEqual(len(_membership_queries(captured)), 1)

    def test_superuser_without_header_needs_no_query(self):
        with self.assertNumQueries(0):
            get_programme_scope(self.admin, _request())

    def test_assert_uses_membership_and_refuses_unknown_ids(self):
        assert_benefit_plan_in_scope(self.member_a, self.plan_a)
        assert_benefit_plan_in_scope(self.member_a, self.plan_none.id)
        assert_benefit_plan_in_scope(self.member_a, None)
        for refused in (self.plan_b, self.plan_b.id, uuid.uuid4(), "not-a-uuid"):
            with self.subTest(refused=refused):
                with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
                    assert_benefit_plan_in_scope(self.member_a, refused)

    def test_assert_with_request_applies_the_header(self):
        request = _request(self.programme_b.id)
        assert_benefit_plan_in_scope(self.member_ab, self.plan_b, request)
        for refused in (self.plan_a, self.plan_none):
            with self.subTest(refused=refused.code):
                with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
                    assert_benefit_plan_in_scope(self.member_ab, refused, request)


class ZeroProgrammeDeploymentTest(TestCase):
    """Without any programme, every user keeps the benefit plans their rights give them."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        cls.user = create_user_with_rights("psZeroUser")
        cls.plans = [
            create_programme_benefit_plan(code, None, cls.admin.username)
            for code in ("PSZ1", "PSZ2")
        ]

    def test_every_benefit_plan_stays_visible(self):
        all_plans = set(BenefitPlan.objects.values_list("id", flat=True))
        visible = set(scope_benefit_plans(BenefitPlan.objects.all(), self.user, _request()).values_list(
            "id", flat=True))
        self.assertEqual(visible, all_plans)

    def test_scope_costs_one_membership_query(self):
        self.assertFalse(Programme.objects.exists())
        with self.assertNumQueries(1):
            get_programme_scope(self.user, _request())

    def test_querysets_are_left_unfiltered(self):
        queryset = Beneficiary.objects.filter(is_deleted=False)
        scoped = scope_queryset(queryset, self.user, _request())
        self.assertEqual(str(scoped.query), str(queryset.query))

    def test_programmes_without_attached_plans_leave_querysets_unfiltered(self):
        programme = create_programme("PSZP", self.admin.username)
        add_member(self.user, programme, self.admin.username)
        queryset = BenefitPlan.objects.all()
        self.assertEqual(str(scope_benefit_plans(queryset, self.user).query), str(queryset.query))

    def test_a_deleted_plan_attached_to_a_programme_restricts_the_scope(self):
        programme = create_programme("PSZD", self.admin.username)
        hidden = create_programme_benefit_plan("PSZH", programme, self.admin.username, is_deleted=True)
        visible = set(scope_benefit_plans(BenefitPlan.objects.all(), self.user).values_list("id", flat=True))
        self.assertNotIn(hidden.id, visible)
        self.assertTrue({plan.id for plan in self.plans} <= visible)


@override_settings(ROW_SECURITY=False)
class ProgrammeModelScopeTest(TestCase):
    """The rule is each model's get_queryset, so every core caller of it follows the programme scope."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        username = cls.admin.username
        cls.programme_a = create_programme("PMA", username)
        cls.programme_b = create_programme("PMB", username)
        cls.member_a = create_user_with_rights("pmMemberA")
        add_member(cls.member_a, cls.programme_a, username)
        cls.plan_a = create_programme_benefit_plan("PMPA", cls.programme_a, username)
        cls.plan_b = create_programme_benefit_plan("PMPB", cls.programme_b, username)
        cls.plan_none = create_programme_benefit_plan("PMPN", None, username)
        cls.group_plan_a = create_programme_benefit_plan("PMGA", cls.programme_a, username, type="GROUP")
        cls.group_plan_b = create_programme_benefit_plan("PMGB", cls.programme_b, username, type="GROUP")
        beneficiaries = BeneficiaryService(cls.admin)
        cls.beneficiary_a = uuid.UUID(str(add_individual_to_benefit_plan(
            beneficiaries, create_individual(username), cls.plan_a)))
        cls.beneficiary_b = uuid.UUID(str(add_individual_to_benefit_plan(
            beneficiaries, create_individual(username), cls.plan_b)))
        groups = GroupBeneficiaryService(cls.admin)
        _, group_a, _ = create_group_with_individual(username)
        _, group_b, _ = create_group_with_individual(username)
        cls.group_beneficiary_a = uuid.UUID(str(add_group_to_benefit_plan(groups, group_a, cls.group_plan_a)))
        cls.group_beneficiary_b = uuid.UUID(str(add_group_to_benefit_plan(groups, group_b, cls.group_plan_b)))
        cls.project_a = create_project("PM project A", cls.plan_a, username)
        cls.project_b = create_project("PM project B", cls.plan_b, username)
        cls.upload_records = {}
        for plan in (cls.plan_a, cls.plan_b):
            upload = IndividualDataSourceUpload(source_name=f"upload {plan.code}", source_type="beneficiary import")
            upload.save(username=username)
            record = BenefitPlanDataUploadRecords(data_upload=upload, benefit_plan=plan, workflow="test")
            record.save(username=username)
            cls.upload_records[plan.code] = record.id

    def _cases(self):
        return (
            (BenefitPlan, self.plan_a.id, self.plan_b.id),
            (Beneficiary, self.beneficiary_a, self.beneficiary_b),
            (GroupBeneficiary, self.group_beneficiary_a, self.group_beneficiary_b),
            (Project, self.project_a.id, self.project_b.id),
            (BenefitPlanDataUploadRecords, self.upload_records["PMPA"], self.upload_records["PMPB"]),
        )

    def test_model_get_queryset_applies_the_programme_scope_without_row_security(self):
        for model, own, foreign in self._cases():
            with self.subTest(model=model.__name__):
                visible = set(model.get_queryset(None, self.member_a).values_list("id", flat=True))
                self.assertIn(own, visible)
                self.assertNotIn(foreign, visible)
                every = set(model.get_queryset(None, self.admin).values_list("id", flat=True))
                self.assertTrue({own, foreign} <= every)

    def test_model_get_queryset_accepts_a_resolve_info(self):
        info = SimpleNamespace(context=SimpleNamespace(user=self.member_a))
        visible = set(BenefitPlan.get_queryset(BenefitPlan.objects.all(), info).values_list("id", flat=True))
        self.assertIn(self.plan_none.id, visible)
        self.assertNotIn(self.plan_b.id, visible)

    def test_anonymous_and_missing_users_get_no_row(self):
        for user in (AnonymousUser(), None):
            for model, own, _ in self._cases():
                with self.subTest(user=user, model=model.__name__):
                    self.assertFalse(model.get_queryset(None, user).filter(id=own).exists())

    def test_core_target_lookup_cannot_reach_a_row_out_of_scope(self):
        for model, own, foreign in self._cases():
            with self.subTest(model=model.__name__):
                self.assertTrue(target_queryset(model, self.member_a).filter(id=own).exists())
                self.assertFalse(target_queryset(model, self.member_a).filter(id=foreign).exists())

    def test_service_target_of_an_update_is_looked_up_in_scope(self):
        with self.assertRaises(ObjectDoesNotExist):
            BeneficiaryService(self.member_a)._get_target(self.beneficiary_b)
        self.assertEqual(BeneficiaryService(self.member_a)._get_target(self.beneficiary_a).id, self.beneficiary_a)

    def test_programmes_and_memberships_are_limited_to_the_users_own(self):
        self.assertEqual(
            set(Programme.get_queryset(None, self.member_a).values_list("code", flat=True)), {"PMA"})
        self.assertEqual(
            set(UserProgramme.get_queryset(None, self.member_a).values_list("user_id", flat=True)),
            {self.member_a.id})
        self.assertFalse(Programme.get_queryset(None, AnonymousUser()).exists())


class ProgrammeRightsTest(TestCase):

    def test_programme_takes_the_benefit_plan_rights(self):
        for action in ("query", "create", "update", "delete"):
            with self.subTest(action=action):
                self.assertTrue(Programme.get_rights(action))
                self.assertEqual(Programme.get_rights(action), BenefitPlan.get_rights(action))

    def test_programme_has_no_close_action(self):
        self.assertIsNone(Programme.get_rights("close"))
