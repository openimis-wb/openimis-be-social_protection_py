import uuid

from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext

from social_protection.models import Beneficiary, BenefitPlan, Programme, Project
from social_protection.programme_scope import (
    INVALID_HEADER_MESSAGE,
    NOT_A_MEMBER_MESSAGE,
    OUT_OF_SCOPE_MESSAGE,
    PROGRAMME_NOT_FOUND_MESSAGE,
    ProgrammeScopeError,
    assert_benefit_plan_in_scope,
    assert_can_set_programme,
    can_access_benefit_plan,
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
from social_protection.tests.test_helpers import create_project


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

    def test_can_access_and_assert_use_membership(self):
        self.assertTrue(can_access_benefit_plan(self.member_a, self.plan_a))
        self.assertTrue(can_access_benefit_plan(self.member_a, self.plan_none.id))
        self.assertFalse(can_access_benefit_plan(self.member_a, self.plan_b))
        self.assertFalse(can_access_benefit_plan(self.member_a, uuid.uuid4()))
        assert_benefit_plan_in_scope(self.member_a, self.plan_a.id)
        assert_benefit_plan_in_scope(self.member_a, uuid.uuid4())
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            assert_benefit_plan_in_scope(self.member_a, self.plan_b.id)

    def test_can_access_with_request_applies_the_header(self):
        request = _request(self.programme_b.id)
        self.assertTrue(can_access_benefit_plan(self.member_ab, self.plan_b, request))
        self.assertFalse(can_access_benefit_plan(self.member_ab, self.plan_a, request))
        self.assertFalse(can_access_benefit_plan(self.member_ab, self.plan_none, request))

    def test_setting_a_programme_requires_membership_of_target_and_current(self):
        assert_can_set_programme(self.member_a, self.programme_a.id)
        assert_can_set_programme(self.member_a, None, self.programme_a.id)
        assert_can_set_programme(self.member_ab, self.programme_b.id, self.programme_a.id)
        with self.assertRaisesMessage(ProgrammeScopeError, NOT_A_MEMBER_MESSAGE):
            assert_can_set_programme(self.member_a, self.programme_b.id)
        with self.assertRaisesMessage(ProgrammeScopeError, NOT_A_MEMBER_MESSAGE):
            assert_can_set_programme(self.member_b, self.programme_b.id, self.programme_a.id)
        with self.assertRaisesMessage(ProgrammeScopeError, NOT_A_MEMBER_MESSAGE):
            assert_can_set_programme(self.member_a, None, self.programme_b.id)

    def test_superuser_sets_any_live_programme_but_not_an_unknown_one(self):
        assert_can_set_programme(self.admin, self.programme_b.id, self.programme_a.id)
        for target in (uuid.uuid4(), self.programme_deleted.id):
            with self.subTest(target=target):
                with self.assertRaisesMessage(ProgrammeScopeError, PROGRAMME_NOT_FOUND_MESSAGE):
                    assert_can_set_programme(self.admin, target)


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
