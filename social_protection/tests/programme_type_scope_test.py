import json

from django.test.utils import override_settings
from graphql_relay import to_global_id

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from individual.models import IndividualDataSourceUpload
from social_protection.models import (
    BenefitPlan,
    BenefitPlanDataUploadRecords,
    BeneficiaryProjectEnrollment,
    BeneficiaryProjectTimeEntry,
    GroupBeneficiaryProjectEnrollment,
    GroupBeneficiaryProjectTimeEntry,
    Project,
)
from social_protection.services import BeneficiaryService, GroupBeneficiaryService
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


@override_settings(ROW_SECURITY=False)
class ProgrammeTypeScopeTest(openIMISGraphQLTestCase):
    """Types owned by a benefit plan are scoped wherever graphene-django resolves them."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        username = cls.admin.username
        cls.admin_token = BaseTestContext(user=cls.admin).get_jwt()
        cls.programme_a = create_programme("PTA", username)
        cls.programme_b = create_programme("PTB", username)
        cls.member = create_user_with_rights("ptMemberA")
        cls.other_member = create_user_with_rights("ptMemberB")
        cls.membership_a = add_member(cls.member, cls.programme_a, username)
        cls.membership_b = add_member(cls.other_member, cls.programme_b, username)
        cls.member_token = BaseTestContext(user=cls.member).get_jwt()

        cls.plan_a = create_programme_benefit_plan("PTPA", cls.programme_a, username)
        cls.plan_b = create_programme_benefit_plan("PTPB", cls.programme_b, username)
        cls.group_plan_a = create_programme_benefit_plan("PTGA", cls.programme_a, username, type="GROUP")
        cls.group_plan_b = create_programme_benefit_plan("PTGB", cls.programme_b, username, type="GROUP")

        individual = create_individual(username, payload_override={"first_name": "PT shared"})
        beneficiaries = BeneficiaryService(cls.admin)
        cls.beneficiary_a = add_individual_to_benefit_plan(
            beneficiaries, individual, cls.plan_a, payload_override={"status": "ACTIVE"})
        cls.beneficiary_b = add_individual_to_benefit_plan(
            beneficiaries, individual, cls.plan_b, payload_override={"status": "ACTIVE"})
        _, group, _ = create_group_with_individual(username)
        groups = GroupBeneficiaryService(cls.admin)
        cls.group_beneficiary_a = add_group_to_benefit_plan(
            groups, group, cls.group_plan_a, payload_override={"status": "ACTIVE"})
        cls.group_beneficiary_b = add_group_to_benefit_plan(
            groups, group, cls.group_plan_b, payload_override={"status": "ACTIVE"})

        cls.project_a = create_project("PT project A", cls.plan_a, username)
        cls.project_b = Project(
            name="PT project B", benefit_plan=cls.plan_b, activity=cls.project_a.activity,
            location=cls.project_a.location, target_beneficiaries=10, working_days=10,
        )
        cls.project_b.save(username=username)
        cls.group_project_b = Project(
            name="PT group project B", benefit_plan=cls.group_plan_b, activity=cls.project_a.activity,
            location=cls.project_a.location, target_beneficiaries=10, working_days=10,
        )
        cls.group_project_b.save(username=username)

        cls.enrollment_b = BeneficiaryProjectEnrollment(beneficiary_id=cls.beneficiary_b, project=cls.project_b)
        cls.enrollment_b.save(username=username)
        cls.cross_enrollment = BeneficiaryProjectEnrollment(
            beneficiary_id=cls.beneficiary_b, project=cls.project_a)
        cls.cross_enrollment.save(username=username)
        cls.time_entry_b = BeneficiaryProjectTimeEntry(enrollment=cls.enrollment_b, day_number=1, percent_complete=10)
        cls.time_entry_b.save(username=username)
        cls.group_enrollment_b = GroupBeneficiaryProjectEnrollment(
            group_beneficiary_id=cls.group_beneficiary_b, project=cls.group_project_b)
        cls.group_enrollment_b.save(username=username)
        cls.group_time_entry_b = GroupBeneficiaryProjectTimeEntry(
            enrollment=cls.group_enrollment_b, day_number=1, percent_complete=10)
        cls.group_time_entry_b.save(username=username)

        cls.enrollment_a = BeneficiaryProjectEnrollment(beneficiary_id=cls.beneficiary_a, project=cls.project_a)
        cls.enrollment_a.save(username=username)
        cls.time_entry_a = BeneficiaryProjectTimeEntry(enrollment=cls.enrollment_a, day_number=1, percent_complete=20)
        cls.time_entry_a.save(username=username)
        cls.foreign_enrollment_of_a = BeneficiaryProjectEnrollment(
            beneficiary_id=cls.beneficiary_a, project=cls.project_b)
        cls.foreign_enrollment_of_a.save(username=username)
        BeneficiaryProjectTimeEntry(
            enrollment=cls.foreign_enrollment_of_a, day_number=1, percent_complete=30).save(username=username)
        cls.group_project_a = Project(
            name="PT group project A", benefit_plan=cls.group_plan_a, activity=cls.project_a.activity,
            location=cls.project_a.location, target_beneficiaries=10, working_days=10,
        )
        cls.group_project_a.save(username=username)
        cls.group_enrollment_a = GroupBeneficiaryProjectEnrollment(
            group_beneficiary_id=cls.group_beneficiary_a, project=cls.group_project_a)
        cls.group_enrollment_a.save(username=username)
        GroupBeneficiaryProjectTimeEntry(
            enrollment=cls.group_enrollment_a, day_number=1, percent_complete=40).save(username=username)
        GroupBeneficiaryProjectEnrollment(
            group_beneficiary_id=cls.group_beneficiary_a, project=cls.group_project_b).save(username=username)

        cls.moved_project = Project(
            name="PT moved project", benefit_plan=cls.plan_b, activity=cls.project_a.activity,
            location=cls.project_a.location, target_beneficiaries=10, working_days=10,
        )
        cls.moved_project.save(username=username)
        cls.moved_project.benefit_plan = cls.plan_a
        cls.moved_project.save(username=username)
        cls.moved_plan = create_programme_benefit_plan("PTMV", cls.programme_b, username)
        cls.moved_plan.programme = cls.programme_a
        cls.moved_plan.save(username=username)

        upload = IndividualDataSourceUpload(source_name="upload PTPB", source_type="beneficiary import")
        upload.save(username=username)
        cls.upload_record_b = BenefitPlanDataUploadRecords(data_upload=upload, benefit_plan=cls.plan_b, workflow="t")
        cls.upload_record_b.save(username=username)

    def _data(self, query, token):
        response = self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertResponseNoErrors(response)
        return json.loads(response.content)["data"]

    def _node(self, type_name, pk, token):
        global_id = to_global_id(type_name, str(pk))
        return self._data('{ node(id: "%s") { id } }' % global_id, token)["node"]

    def _out_of_scope_nodes(self):
        return [
            ("BenefitPlanGQLType", self.plan_b.id),
            ("BeneficiaryGQLType", self.beneficiary_b),
            ("GroupBeneficiaryGQLType", self.group_beneficiary_b),
            ("ProjectGQLType", self.project_b.id),
            ("BenefitPlanDataUploadQGLType", self.upload_record_b.id),
            ("BeneficiaryProjectEnrollmentGQLType", self.enrollment_b.id),
            ("BeneficiaryProjectEnrollmentGQLType", self.cross_enrollment.id),
            ("GroupBeneficiaryProjectEnrollmentGQLType", self.group_enrollment_b.id),
            ("BeneficiaryProjectTimeEntryGQLType", self.time_entry_b.id),
            ("GroupBeneficiaryProjectTimeEntryGQLType", self.group_time_entry_b.id),
            ("ProgrammeGQLType", self.programme_b.id),
            ("UserProgrammeGQLType", self.membership_b.id),
            ("BenefitPlanHistoryGQLType", BenefitPlan.history.filter(id=self.plan_b.id).first().history_id),
            ("ProjectHistoryGQLType", Project.history.filter(id=self.project_b.id).first().history_id),
        ]

    def test_node_hides_rows_out_of_scope(self):
        for type_name, pk in self._out_of_scope_nodes():
            with self.subTest(type_name=type_name):
                self.assertIsNone(self._node(type_name, pk, self.member_token))
                self.assertIsNotNone(self._node(type_name, pk, self.admin_token))

    def test_node_shows_rows_in_scope(self):
        for type_name, pk in (
            ("BenefitPlanGQLType", self.plan_a.id),
            ("BeneficiaryGQLType", self.beneficiary_a),
            ("ProjectGQLType", self.project_a.id),
            ("ProgrammeGQLType", self.programme_a.id),
            ("UserProgrammeGQLType", self.membership_a.id),
        ):
            with self.subTest(type_name=type_name):
                self.assertIsNotNone(self._node(type_name, pk, self.member_token))

    def test_history_nodes_cannot_be_enumerated_by_integer_id(self):
        for type_name, history, own_id, foreign_id in (
            ("BenefitPlanHistoryGQLType", BenefitPlan.history, self.plan_a.id, self.plan_b.id),
            ("ProjectHistoryGQLType", Project.history, self.project_a.id, self.project_b.id),
        ):
            own = set(history.filter(id=own_id).values_list("history_id", flat=True))
            foreign = set(history.filter(id=foreign_id).values_list("history_id", flat=True))
            for history_id in range(min(own | foreign), max(own | foreign) + 1):
                with self.subTest(type_name=type_name, history_id=history_id):
                    node = self._node(type_name, history_id, self.member_token)
                    if history_id in foreign:
                        self.assertIsNone(node)
                    if history_id in own:
                        self.assertIsNotNone(node)

    def test_user_created_pivot_is_scoped(self):
        query = """{ benefitPlan(code: "PTPA") { edges { node { userCreated {
            benefitplanUserCreated { edges { node { code } } }
            projectUserCreated { edges { node { name } } }
            beneficiaryUserCreated { edges { node { benefitPlan { code } } } }
            groupbeneficiaryUserCreated { edges { node { benefitPlan { code } } } }
            programmeUserCreated { edges { node { code } } }
        } } } } }"""
        user = self._data(query, self.member_token)["benefitPlan"]["edges"][0]["node"]["userCreated"]
        codes = {e["node"]["code"] for e in user["benefitplanUserCreated"]["edges"]}
        self.assertIn("PTPA", codes)
        self.assertNotIn("PTPB", codes)
        self.assertNotIn("PT project B", {e["node"]["name"] for e in user["projectUserCreated"]["edges"]})
        self.assertNotIn("PTPB", {e["node"]["benefitPlan"]["code"] for e in user["beneficiaryUserCreated"]["edges"]})
        self.assertNotIn(
            "PTGB", {e["node"]["benefitPlan"]["code"] for e in user["groupbeneficiaryUserCreated"]["edges"]})
        self.assertNotIn("PTB", {e["node"]["code"] for e in user["programmeUserCreated"]["edges"]})

    def test_individual_and_group_pivots_are_scoped(self):
        query = """{ beneficiary(benefitPlan_Code: "PTPA") { edges { node { individual {
            beneficiarySet { edges { node { benefitPlan { code } } } } } } } } }"""
        node = self._data(query, self.member_token)["beneficiary"]["edges"][0]["node"]
        codes = {e["node"]["benefitPlan"]["code"] for e in node["individual"]["beneficiarySet"]["edges"]}
        self.assertEqual(codes, {"PTPA"})
        query = """{ groupBeneficiary(benefitPlan_Code: "PTGA") { edges { node { group {
            groupbeneficiarySet { edges { node { benefitPlan { code } } } } } } } } }"""
        node = self._data(query, self.member_token)["groupBeneficiary"]["edges"][0]["node"]
        codes = {e["node"]["benefitPlan"]["code"] for e in node["group"]["groupbeneficiarySet"]["edges"]}
        self.assertEqual(codes, {"PTGA"})

    def test_activity_and_location_pivots_are_scoped(self):
        query = """{ project(name: "PT project A") { edges { node {
            activity { projectSet { edges { node { name } } } }
            location { projectSet { edges { node { name } } } }
        } } } }"""
        node = self._data(query, self.member_token)["project"]["edges"][0]["node"]
        for pivot in ("activity", "location"):
            with self.subTest(pivot=pivot):
                names = {e["node"]["name"] for e in node[pivot]["projectSet"]["edges"]}
                self.assertIn("PT project A", names)
                self.assertNotIn("PT project B", names)
                self.assertNotIn("PT group project B", names)

    def test_project_enrollments_pivot_is_scoped(self):
        query = """{ project(name: "PT project A") { edges { node {
            beneficiaryEnrollments { edges { node { id beneficiary { benefitPlan { code } } } } } } } } }"""
        node = self._data(query, self.member_token)["project"]["edges"][0]["node"]
        enrollments = {
            e["node"]["id"]: e["node"]["beneficiary"]["benefitPlan"]["code"]
            for e in node["beneficiaryEnrollments"]["edges"]
        }
        self.assertIn(to_global_id("BeneficiaryProjectEnrollmentGQLType", str(self.enrollment_a.id)), enrollments)
        self.assertNotIn(
            to_global_id("BeneficiaryProjectEnrollmentGQLType", str(self.cross_enrollment.id)), enrollments)
        self.assertEqual(set(enrollments.values()), {"PTPA"})

    def test_beneficiary_project_enrollments_and_time_entries_are_scoped(self):
        query = """{ beneficiary(benefitPlan_Code: "PTPA") { edges { node {
            projectEnrollments { project { name benefitPlan { code } } timeEntries { percentComplete } } } } } }"""
        node = self._data(query, self.member_token)["beneficiary"]["edges"][0]["node"]
        projects = {e["project"]["name"] for e in node["projectEnrollments"]}
        self.assertIn("PT project A", projects)
        self.assertNotIn("PT project B", projects)
        entries = {t["percentComplete"] for e in node["projectEnrollments"] for t in e["timeEntries"]}
        self.assertEqual(entries, {20})

    def test_group_beneficiary_project_enrollments_and_time_entries_are_scoped(self):
        query = """{ groupBeneficiary(benefitPlan_Code: "PTGA") { edges { node {
            projectEnrollments { project { name } timeEntries { percentComplete } } } } } }"""
        node = self._data(query, self.member_token)["groupBeneficiary"]["edges"][0]["node"]
        projects = {e["project"]["name"] for e in node["projectEnrollments"]}
        self.assertIn("PT group project A", projects)
        self.assertNotIn("PT group project B", projects)
        entries = {t["percentComplete"] for e in node["projectEnrollments"] for t in e["timeEntries"]}
        self.assertEqual(entries, {40})

    def test_history_of_a_moved_project_hides_its_former_benefit_plan(self):
        query = '{ projectHistory(id: "%s") { edges { node { benefitPlan { code } } } } }' % self.moved_project.id
        rows = self._data(query, self.member_token)["projectHistory"]["edges"]
        plans = [row["node"]["benefitPlan"] for row in rows]
        self.assertEqual(len(plans), 2)
        self.assertIn({"code": "PTPA"}, plans)
        self.assertIn(None, plans)

    def test_history_of_a_moved_plan_hides_its_former_programme(self):
        query = '{ benefitPlanHistory(id: "%s") { edges { node { programme { code } } } } }' % self.moved_plan.id
        rows = self._data(query, self.member_token)["benefitPlanHistory"]["edges"]
        programmes = [row["node"]["programme"] for row in rows]
        self.assertIn({"code": "PTA"}, programmes)
        self.assertIn(None, programmes)
        self.assertNotIn({"code": "PTB"}, programmes)

    def test_eligible_for_project_filter_treats_a_foreign_project_as_unknown(self):
        query = '{ beneficiary(eligibleForProject: "%s") { totalCount } }'
        own = self.query(query % self.project_a.id, headers={"HTTP_AUTHORIZATION": f"Bearer {self.member_token}"})
        self.assertResponseNoErrors(own)
        foreign = self.query(query % self.project_b.id, headers={"HTTP_AUTHORIZATION": f"Bearer {self.member_token}"})
        self.assertIn("errors", json.loads(foreign.content))
