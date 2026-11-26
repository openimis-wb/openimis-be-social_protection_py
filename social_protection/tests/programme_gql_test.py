import json
import uuid
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext, override_settings
from graphql_relay import to_global_id

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from individual.models import IndividualDataSourceUpload
from social_protection import schema as sp_schema
from social_protection.models import (
    BenefitPlan, BenefitPlanDataUploadRecords, Programme, UserProgramme,
)
from social_protection.programme_scope import (
    INVALID_HEADER_MESSAGE,
    NOT_A_MEMBER_MESSAGE,
    OUT_OF_SCOPE_MESSAGE,
    PROGRAMME_NOT_FOUND_MESSAGE,
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


class ProgrammeGQLTest(openIMISGraphQLTestCase):

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        username = cls.admin.username
        cls.admin_token = BaseTestContext(user=cls.admin).get_jwt()

        cls.programme_a = create_programme("PGA", username)
        cls.programme_b = create_programme("PGB", username)
        cls.programme_empty = create_programme("PGE", username)
        cls.programme_deleted = create_programme("PGDEL", username, is_deleted=True)

        cls.member_a = create_user_with_rights("pgMemberA")
        cls.member_ab = create_user_with_rights("pgMemberAB")
        cls.no_membership = create_user_with_rights("pgNoMember")
        cls.no_rights = create_user_with_rights("pgNoRights", right_ids=[])
        add_member(cls.member_a, cls.programme_a, username)
        add_member(cls.member_a, cls.programme_empty, username)
        add_member(cls.member_ab, cls.programme_a, username)
        add_member(cls.member_ab, cls.programme_b, username)
        add_member(cls.no_rights, cls.programme_a, username)
        cls.member_a_token = BaseTestContext(user=cls.member_a).get_jwt()
        cls.member_ab_token = BaseTestContext(user=cls.member_ab).get_jwt()
        cls.no_membership_token = BaseTestContext(user=cls.no_membership).get_jwt()
        cls.no_rights_token = BaseTestContext(user=cls.no_rights).get_jwt()

        cls.plan_a = create_programme_benefit_plan("PGPA", cls.programme_a, username)
        cls.plan_b = create_programme_benefit_plan("PGPB", cls.programme_b, username, beneficiary_data_schema={
            "properties": {"only_in_b": {"type": "string"}}
        })
        cls.plan_none = create_programme_benefit_plan("PGPN", None, username)
        cls.group_plan_a = create_programme_benefit_plan("PGGA", cls.programme_a, username, type="GROUP")
        cls.group_plan_b = create_programme_benefit_plan("PGGB", cls.programme_b, username, type="GROUP")

        beneficiary_service = BeneficiaryService(cls.admin)
        for plan in (cls.plan_a, cls.plan_b, cls.plan_none):
            individual = create_individual(username, payload_override={"first_name": f"Ind {plan.code}"})
            add_individual_to_benefit_plan(beneficiary_service, individual, plan)
        group_service = GroupBeneficiaryService(cls.admin)
        for plan in (cls.group_plan_a, cls.group_plan_b):
            _, group, _ = create_group_with_individual(username)
            add_group_to_benefit_plan(group_service, group, plan)

        for plan in (cls.plan_a, cls.plan_b):
            upload = IndividualDataSourceUpload(source_name=f"upload {plan.code}", source_type="beneficiary import")
            upload.save(username=username)
            BenefitPlanDataUploadRecords(data_upload=upload, benefit_plan=plan, workflow="test").save(
                username=username)

        cls.project_a = create_project("PG project A", cls.plan_a, username)
        cls.project_b = create_project("PG project B", cls.plan_b, username)

    def _query(self, query, token, programme_id=None, variables=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if programme_id is not None:
            headers["HTTP_X_PROGRAMME"] = str(programme_id)
        return self.query(query, headers=headers, variables=variables)

    def _data(self, response):
        self.assertResponseNoErrors(response)
        return json.loads(response.content)["data"]

    def _codes(self, response, field, path=("code",)):
        codes = set()
        for edge in self._data(response)[field]["edges"]:
            value = edge["node"]
            for key in path:
                value = value[key]
            codes.add(value)
        return codes

    def _mutate(self, mutation_name, input_body, token):
        response = self._query(
            f"mutation {{ {mutation_name}(input: {{ {input_body} }}) {{ internalId }} }}", token
        )
        return self._data(response)[mutation_name]["internalId"]

    # Reads

    def test_benefit_plan_query_is_scoped_by_membership(self):
        query = "{ benefitPlan { edges { node { code programme { code } } } } }"
        member_codes = self._codes(self._query(query, self.member_a_token), "benefitPlan")
        self.assertIn("PGPA", member_codes)
        self.assertIn("PGPN", member_codes)
        self.assertNotIn("PGPB", member_codes)
        self.assertNotIn("PGGB", member_codes)
        admin_codes = self._codes(self._query(query, self.admin_token), "benefitPlan")
        self.assertTrue({"PGPA", "PGPB", "PGPN"} <= admin_codes)

    def test_benefit_plan_exposes_programme_and_filters_on_it(self):
        response = self._query(
            '{ benefitPlan(programme_Id: "%s") { edges { node { code programme { code uuid } } } } }'
            % self.programme_a.id, self.admin_token
        )
        edges = self._data(response)["benefitPlan"]["edges"]
        self.assertEqual({e["node"]["code"] for e in edges}, {"PGPA", "PGGA"})
        self.assertEqual({e["node"]["programme"]["uuid"] for e in edges}, {str(self.programme_a.id)})

    def test_header_narrows_benefit_plans_to_the_active_programme(self):
        query = "{ benefitPlan { edges { node { code } } } }"
        codes = self._codes(self._query(query, self.member_ab_token, self.programme_b.id), "benefitPlan")
        self.assertEqual(codes, {"PGPB", "PGGB"})

    @override_settings(ROW_SECURITY=False)
    def test_header_narrows_nodes_and_owned_rows(self):
        node_query = '{ node(id: "%s") { id } }' % to_global_id("BenefitPlanGQLType", str(self.plan_a.id))
        self.assertIsNone(self._data(self._query(node_query, self.member_ab_token, self.programme_b.id))["node"])
        self.assertIsNotNone(self._data(self._query(node_query, self.member_ab_token, self.programme_a.id))["node"])
        names = self._codes(
            self._query("{ project { edges { node { name } } } }", self.member_ab_token, self.programme_b.id),
            "project", ("name",),
        )
        self.assertIn("PG project B", names)
        self.assertNotIn("PG project A", names)

    def test_invalid_header_is_refused_with_a_clear_error(self):
        query = "{ benefitPlan { edges { node { code } } } }"
        for value in (self.programme_b.id, uuid.uuid4(), "garbage", self.programme_deleted.id):
            with self.subTest(value=value):
                response = self._query(query, self.member_a_token, value)
                content = json.loads(response.content)
                self.assertIn("errors", content)
                self.assertIn(INVALID_HEADER_MESSAGE, content["errors"][0]["message"])

    def test_benefit_plan_history_is_scoped(self):
        codes = self._codes(
            self._query("{ benefitPlanHistory { edges { node { code } } } }", self.member_a_token),
            "benefitPlanHistory",
        )
        self.assertIn("PGPA", codes)
        self.assertNotIn("PGPB", codes)

    @override_settings(ROW_SECURITY=False)
    def test_beneficiary_query_is_scoped(self):
        query = "{ beneficiary { edges { node { benefitPlan { code } } } } }"
        codes = self._codes(self._query(query, self.member_a_token), "beneficiary", ("benefitPlan", "code"))
        self.assertTrue({"PGPA", "PGPN"} <= codes)
        self.assertNotIn("PGPB", codes)
        codes = self._codes(
            self._query(query, self.member_ab_token, self.programme_b.id), "beneficiary", ("benefitPlan", "code"))
        self.assertEqual(codes, {"PGPB"})

    @override_settings(ROW_SECURITY=False)
    def test_group_beneficiary_query_is_scoped(self):
        query = "{ groupBeneficiary { edges { node { benefitPlan { code } } } } }"
        codes = self._codes(self._query(query, self.member_a_token), "groupBeneficiary", ("benefitPlan", "code"))
        self.assertIn("PGGA", codes)
        self.assertNotIn("PGGB", codes)

    @override_settings(ROW_SECURITY=False)
    def test_beneficiary_export_is_scoped(self):
        captured = {}

        def _capture(queryset, *args, **kwargs):
            captured["codes"] = set(queryset.values_list("benefit_plan__code", flat=True))
            return SimpleNamespace(name="export")

        request = RequestFactory().get("/")
        request.user = self.member_a
        with mock.patch.object(sp_schema.gql_optimizer, "query", side_effect=lambda qs, info: qs), \
                mock.patch("social_protection.export_mixin.ExportableQueryModel.create_csv_export",
                           side_effect=_capture):
            sp_schema.Query.resolve_beneficiary_export(
                None, SimpleNamespace(context=request),
                fields=["id"], fields_columns=json.dumps({"id": "ID"}),
            )
        self.assertIn("PGPA", captured["codes"])
        self.assertNotIn("PGPB", captured["codes"])

    @override_settings(ROW_SECURITY=False)
    def test_export_refuses_field_paths_through_reverse_relations(self):
        request = RequestFactory().get("/")
        request.user = self.member_a
        refused = (
            ["individual.beneficiary.benefitPlan.code"],
            ["benefitPlan.beneficiary.individual.firstName"],
            ["individual.groupindividuals.group.code"],
        )
        with mock.patch.object(sp_schema.gql_optimizer, "query", side_effect=lambda qs, info: qs), \
                mock.patch("social_protection.export_mixin.ExportableQueryModel.create_csv_export") as export:
            for fields in refused:
                with self.subTest(fields=fields):
                    with self.assertRaisesMessage(ValueError, "social_protection.export.field_not_allowed"):
                        sp_schema.Query.resolve_beneficiary_export(
                            None, SimpleNamespace(context=request),
                            fields=fields, fields_columns=json.dumps({}),
                        )
            export.assert_not_called()
            export.return_value = SimpleNamespace(name="export")
            sp_schema.Query.resolve_beneficiary_export(
                None, SimpleNamespace(context=request),
                fields=["individual.firstName", "benefitPlan.code", "benefitPlan.programme.code",
                        "jsonExt.educated_level", "status"],
                fields_columns=json.dumps({}),
            )
            export.assert_called_once()

    @override_settings(ROW_SECURITY=False)
    def test_group_beneficiary_export_is_scoped(self):
        captured = {}

        def _capture(queryset, *args, **kwargs):
            captured["codes"] = set(queryset.values_list("benefit_plan__code", flat=True))
            return SimpleNamespace(name="export")

        request = RequestFactory().get("/")
        request.user = self.member_a
        with mock.patch.object(sp_schema.gql_optimizer, "query", side_effect=lambda qs, info: qs), \
                mock.patch("social_protection.export_mixin.ExportableQueryModel.create_csv_export",
                           side_effect=_capture):
            sp_schema.Query.resolve_group_beneficiary_export(
                None, SimpleNamespace(context=request),
                fields=["id", "group.id", "json_ext"], fields_columns=json.dumps({"id": "ID"}),
            )
        self.assertIn("PGGA", captured["codes"])
        self.assertNotIn("PGGB", captured["codes"])

    @override_settings(ROW_SECURITY=False)
    def test_export_refuses_paths_into_user_models_and_accepts_frontend_paths(self):
        request = RequestFactory().get("/")
        request.user = self.member_a
        info = SimpleNamespace(context=request)
        with mock.patch.object(sp_schema.gql_optimizer, "query", side_effect=lambda qs, info: qs), \
                mock.patch("social_protection.export_mixin.ExportableQueryModel.create_csv_export") as export:
            export.return_value = SimpleNamespace(name="export")
            for fields in (["userCreated.iUser.password"], ["userCreated.iUser.privateKey"],
                           ["individual.userCreated.username"], ["benefitPlan.userUpdated.iUser.email"],
                           ["userCreated.username"]):
                with self.subTest(fields=fields):
                    with self.assertRaisesMessage(ValueError, "social_protection.export.field_not_allowed"):
                        sp_schema.Query.resolve_beneficiary_export(
                            None, info, fields=fields, fields_columns=json.dumps({}))
            export.assert_not_called()
            sp_schema.Query.resolve_beneficiary_export(
                None, info,
                fields=["id", "individual.first_name", "individual.last_name", "individual.dob", "json_ext",
                        "jsonExt.educated_level", "individual.location.parent.name"],
                fields_columns=json.dumps({}),
            )
            sp_schema.Query.resolve_group_beneficiary_export(
                None, info, fields=["id", "group.id", "json_ext"], fields_columns=json.dumps({}))
            self.assertEqual(export.call_count, 2)

    @override_settings(ROW_SECURITY=False)
    def test_export_source_is_scoped_on_its_model(self):
        from social_protection.export_mixin import ExportableSocialProtectionQueryMixin
        from social_protection.models import Beneficiary
        request = RequestFactory().get("/")
        request.user = self.member_a
        info = SimpleNamespace(context=request)
        for source in (Beneficiary.objects, Beneficiary.objects.all()):
            with self.subTest(source=type(source).__name__):
                rows = ExportableSocialProtectionQueryMixin._exportable_queryset(source, ["id"], info)
                codes = set(rows.values_list("benefit_plan__code", flat=True))
                self.assertIn("PGPA", codes)
                self.assertNotIn("PGPB", codes)
                with self.assertRaisesMessage(ValueError, "social_protection.export.field_not_allowed"):
                    ExportableSocialProtectionQueryMixin._exportable_queryset(
                        source, ["user_created__username"], info)

    def test_upload_history_is_scoped(self):
        codes = self._codes(
            self._query("{ beneficiaryDataUploadHistory { edges { node { benefitPlan { code } } } } }",
                        self.member_a_token),
            "beneficiaryDataUploadHistory", ("benefitPlan", "code"),
        )
        self.assertIn("PGPA", codes)
        self.assertNotIn("PGPB", codes)

    @override_settings(ROW_SECURITY=False)
    def test_project_and_project_history_are_scoped(self):
        for field in ("project", "projectHistory"):
            with self.subTest(field=field):
                names = self._codes(
                    self._query("{ %s { edges { node { name } } } }" % field, self.member_a_token),
                    field, ("name",),
                )
                self.assertIn("PG project A", names)
                self.assertNotIn("PG project B", names)

    def test_schema_fields_are_scoped(self):
        query = "{ benefitPlanSchemaField { schemaFields } }"
        member_fields = self._data(self._query(query, self.member_a_token))["benefitPlanSchemaField"]["schemaFields"]
        admin_fields = self._data(self._query(query, self.admin_token))["benefitPlanSchemaField"]["schemaFields"]
        self.assertIn("json_ext__only_in_b", admin_fields)
        self.assertNotIn("json_ext__only_in_b", member_fields)

    def test_membership_is_resolved_once_per_request(self):
        query = """{
          benefitPlan { totalCount }
          project { totalCount }
          beneficiaryDataUploadHistory { totalCount }
          benefitPlanHistory { totalCount }
        }"""
        with CaptureQueriesContext(connection) as captured:
            self._data(self._query(query, self.member_ab_token, self.programme_a.id))
        membership_queries = [
            q for q in captured.captured_queries if "social_protection_userprogramme" in q["sql"]
        ]
        self.assertEqual(len(membership_queries), 1)

    def test_my_programmes_ignores_a_stale_header(self):
        query = "{ myProgrammes { isDefault programme { uuid code } } }"
        response = self._query(query, self.member_a_token, self.programme_b.id)
        codes = {row["programme"]["code"] for row in self._data(response)["myProgrammes"]}
        self.assertEqual(codes, {"PGA", "PGE"})

    def test_my_programmes_requires_authentication(self):
        response = self.query("{ myProgrammes { isDefault } }")
        self.assertIn("errors", json.loads(response.content))

    def test_programme_query_shows_own_programmes_to_members_and_all_to_superusers(self):
        query = "{ programme { edges { node { code } } } }"
        self.assertEqual(self._codes(self._query(query, self.member_a_token), "programme"), {"PGA", "PGE"})
        self.assertEqual(self._codes(self._query(query, self.no_membership_token), "programme"), set())
        admin_codes = self._codes(self._query('{ programme(code_Istartswith: "PG") { edges { node { code } } } }',
                                              self.admin_token), "programme")
        self.assertEqual(admin_codes, {"PGA", "PGB", "PGE", "PGDEL"})

    def test_programme_members_query(self):
        query = '{ programmeMembers(programmeId: "%s") { edges { node { username userId isDefault } } } }'
        usernames = self._codes(
            self._query(query % self.programme_a.id, self.member_a_token), "programmeMembers", ("username",))
        self.assertEqual(usernames, {"pgMemberA", "pgMemberAB", "pgNoRights"})
        response = self._query(query % self.programme_b.id, self.member_a_token)
        self.assertIn(NOT_A_MEMBER_MESSAGE, json.loads(response.content)["errors"][0]["message"])
        response = self._query(query % self.programme_a.id, self.no_rights_token)
        self.assertIn("errors", json.loads(response.content))

    # Benefit plan writes

    def _benefit_plan_input(self, code, programme_id=None, plan_id=None):
        body = (f'code: "{code}", name: "Name {code}", type: INDIVIDUAL, '
                f'dateValidFrom: "2024-01-01", dateValidTo: "2030-12-31"')
        if programme_id is not None:
            body += f', programmeId: "{programme_id}"'
        if plan_id is not None:
            body += f', id: "{plan_id}"'
        return body

    def test_create_benefit_plan_in_a_programme_requires_membership(self):
        internal_id = self._mutate(
            "createBenefitPlan", self._benefit_plan_input("PGC1", self.programme_b.id), self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, NOT_A_MEMBER_MESSAGE)
        self.assertFalse(BenefitPlan.objects.filter(code="PGC1").exists())

        internal_id = self._mutate(
            "createBenefitPlan", self._benefit_plan_input("PGC2", uuid.uuid4()), self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, PROGRAMME_NOT_FOUND_MESSAGE)

        internal_id = self._mutate(
            "createBenefitPlan", self._benefit_plan_input("PGC3", self.programme_a.id), self.member_a_token)
        self.assert_mutation_success(internal_id, self.member_a_token)
        self.assertEqual(BenefitPlan.objects.get(code="PGC3").programme_id, self.programme_a.id)

    def test_update_of_an_out_of_scope_benefit_plan_is_refused(self):
        internal_id = self._mutate(
            "updateBenefitPlan", self._benefit_plan_input("PGPB", plan_id=self.plan_b.id), self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, OUT_OF_SCOPE_MESSAGE)

    def test_update_benefit_plan_input_has_no_programme_fields(self):
        for extra in (f', programmeId: "{self.programme_b.id}"', ", detachProgramme: true"):
            with self.subTest(extra=extra):
                response = self._query(
                    "mutation { updateBenefitPlan(input: { %s }) { internalId } }"
                    % (self._benefit_plan_input("PGPA", plan_id=self.plan_a.id) + extra),
                    self.admin_token,
                )
                self.assertIn("errors", json.loads(response.content))

    def test_update_with_maker_checker_leaves_the_programme_alone(self):
        from tasks_management.models import Task
        internal_id = self._mutate(
            "updateBenefitPlan", self._benefit_plan_input("PGPA", plan_id=self.plan_a.id), self.admin_token)
        self.assert_mutation_success(internal_id, self.admin_token)
        task = Task.objects.filter(entity_id=str(self.plan_a.id)).order_by("-date_created").first()
        self.assertIsNotNone(task)
        self.assertNotIn("programme_id", json.dumps(task.data))
        self.assertEqual(BenefitPlan.objects.get(id=self.plan_a.id).programme_id, self.programme_a.id)

    def _set_programme(self, plan, programme_id, token):
        body = f'id: "{plan.id}"' + (f', programmeId: "{programme_id}"' if programme_id else "")
        return self._mutate("setBenefitPlanProgramme", body, token)

    def test_superuser_sets_the_programme_of_an_existing_plan_immediately(self):
        internal_id = self._set_programme(self.plan_a, self.programme_b.id, self.admin_token)
        self.assert_mutation_success(internal_id, self.admin_token)
        self.assertEqual(BenefitPlan.objects.get(id=self.plan_a.id).programme_id, self.programme_b.id)

        internal_id = self._set_programme(self.plan_a, self.programme_b.id, self.admin_token)
        self.assert_mutation_success(internal_id, self.admin_token)
        self.assertEqual(BenefitPlan.objects.get(id=self.plan_a.id).programme_id, self.programme_b.id)

        internal_id = self._set_programme(self.plan_a, None, self.admin_token)
        self.assert_mutation_success(internal_id, self.admin_token)
        self.assertIsNone(BenefitPlan.objects.get(id=self.plan_a.id).programme_id)

    def test_setting_an_unknown_or_deleted_programme_is_refused(self):
        for target in (uuid.uuid4(), self.programme_deleted.id):
            with self.subTest(target=target):
                internal_id = self._set_programme(self.plan_a, target, self.admin_token)
                self.assert_mutation_error(internal_id, self.admin_token, PROGRAMME_NOT_FOUND_MESSAGE)
                self.assertEqual(BenefitPlan.objects.get(id=self.plan_a.id).programme_id, self.programme_a.id)

    def test_setting_the_programme_is_refused_to_non_superusers(self):
        for plan, target in ((self.plan_a, self.programme_empty.id), (self.plan_a, None),
                             (self.plan_none, self.programme_a.id)):
            with self.subTest(plan=plan.code, target=target):
                internal_id = self._set_programme(plan, target, self.member_a_token)
                self.assert_mutation_error(
                    internal_id, self.member_a_token, "social_protection.programme.change_requires_superuser")
                self.assertEqual(BenefitPlan.objects.get(id=plan.id).programme_id, plan.programme_id)

    def test_bulk_delete_rolls_back_earlier_ids_when_a_later_id_is_refused(self):
        internal_id = self._mutate(
            "deleteBenefitPlan", f'ids: ["{self.plan_a.id}", "{self.plan_b.id}"]', self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, OUT_OF_SCOPE_MESSAGE)
        self.assertFalse(BenefitPlan.objects.get(id=self.plan_a.id).is_deleted)
        self.assertFalse(BenefitPlan.objects.get(id=self.plan_b.id).is_deleted)

    def test_delete_of_an_out_of_scope_benefit_plan_is_refused(self):
        internal_id = self._mutate("deleteBenefitPlan", f'ids: ["{self.plan_b.id}"]', self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, OUT_OF_SCOPE_MESSAGE)
        self.assertFalse(BenefitPlan.objects.get(id=self.plan_b.id).is_deleted)

    # Programme administration

    def test_create_programme_makes_a_non_superuser_creator_member(self):
        internal_id = self._mutate(
            "createProgramme", 'code: "PGNEW", name: "New programme"', self.member_a_token)
        self.assert_mutation_success(internal_id, self.member_a_token)
        programme = Programme.objects.get(code="PGNEW", is_deleted=False)
        self.assertTrue(UserProgramme.objects.filter(
            user_id=self.member_a.id, programme=programme, is_deleted=False).exists())

    def test_create_programme_with_a_used_code_is_refused(self):
        internal_id = self._mutate("createProgramme", 'code: "PGA", name: "Duplicate"', self.admin_token)
        self.assert_mutation_error(internal_id, self.admin_token, "programme.code_exists")

    def test_programme_mutations_require_the_programme_rights(self):
        internal_id = self._mutate("createProgramme", 'code: "PGNR", name: "No rights"', self.no_rights_token)
        self.assert_mutation_error(internal_id, self.no_rights_token, "authentication_required")

    def test_update_programme_requires_membership(self):
        internal_id = self._mutate(
            "updateProgramme", f'id: "{self.programme_b.id}", name: "Renamed"', self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, NOT_A_MEMBER_MESSAGE)

        internal_id = self._mutate(
            "updateProgramme", f'id: "{self.programme_a.id}", name: "Renamed A"', self.member_a_token)
        self.assert_mutation_success(internal_id, self.member_a_token)
        self.assertEqual(Programme.objects.get(id=self.programme_a.id).name, "Renamed A")

    def test_delete_programme_is_refused_while_a_live_benefit_plan_is_attached(self):
        internal_id = self._mutate("deleteProgramme", f'ids: ["{self.programme_a.id}"]', self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, "programme.has_benefit_plans")
        self.assertFalse(Programme.objects.get(id=self.programme_a.id).is_deleted)

        internal_id = self._mutate(
            "deleteProgramme", f'ids: ["{self.programme_empty.id}"]', self.member_a_token)
        self.assert_mutation_success(internal_id, self.member_a_token)
        self.assertTrue(Programme.objects.get(id=self.programme_empty.id).is_deleted)

    def test_delete_programme_is_refused_while_a_deleted_benefit_plan_references_it(self):
        programme = create_programme("PGX", self.admin.username)
        add_member(self.member_a, programme, self.admin.username)
        create_programme_benefit_plan("PGXD", programme, self.admin.username, is_deleted=True)
        internal_id = self._mutate("deleteProgramme", f'ids: ["{programme.id}"]', self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, "programme.has_benefit_plans")
        self.assertFalse(Programme.objects.get(id=programme.id).is_deleted)

    def test_programme_connections_have_a_default_order(self):
        response = self._query('{ programme(code_Istartswith: "PG") { edges { node { code } } } }', self.admin_token)
        codes = [e["node"]["code"] for e in self._data(response)["programme"]["edges"]]
        self.assertEqual(codes, sorted(codes))
        late_member = create_user_with_rights("pgAaaLateMember")
        add_member(late_member, self.programme_a, self.admin.username)
        response = self._query(
            '{ programmeMembers(programmeId: "%s") { edges { node { username } } } }' % self.programme_a.id,
            self.admin_token)
        usernames = [e["node"]["username"] for e in self._data(response)["programmeMembers"]["edges"]]
        self.assertEqual(usernames, sorted(usernames))
        self.assertEqual(usernames[0], "pgAaaLateMember")

    def test_members_are_managed_only_inside_own_programmes(self):
        internal_id = self._mutate(
            "addProgrammeMembers",
            f'programmeId: "{self.programme_b.id}", userIds: ["{self.member_a.id}"]',
            self.member_a_token,
        )
        self.assert_mutation_error(internal_id, self.member_a_token, NOT_A_MEMBER_MESSAGE)
        self.assertFalse(UserProgramme.objects.filter(
            user_id=self.member_a.id, programme=self.programme_b, is_deleted=False).exists())

        internal_id = self._mutate(
            "addProgrammeMembers",
            f'programmeId: "{self.programme_a.id}", userIds: ["{self.no_membership.id}"]',
            self.member_a_token,
        )
        self.assert_mutation_success(internal_id, self.member_a_token)
        self.assertTrue(UserProgramme.objects.filter(
            user_id=self.no_membership.id, programme=self.programme_a, is_deleted=False).exists())

        internal_id = self._mutate(
            "removeProgrammeMembers",
            f'programmeId: "{self.programme_a.id}", userIds: ["{self.member_ab.id}"]',
            self.member_a_token,
        )
        self.assert_mutation_success(internal_id, self.member_a_token)
        self.assertFalse(UserProgramme.objects.filter(
            user_id=self.member_ab.id, programme=self.programme_a, is_deleted=False).exists())

    def test_set_my_default_programme(self):
        internal_id = self._mutate(
            "setMyDefaultProgramme", f'programmeId: "{self.programme_a.id}"', self.member_a_token)
        self.assert_mutation_success(internal_id, self.member_a_token)
        internal_id = self._mutate(
            "setMyDefaultProgramme", f'programmeId: "{self.programme_empty.id}"', self.member_a_token)
        self.assert_mutation_success(internal_id, self.member_a_token)

        rows = self._data(self._query(
            "{ myProgrammes { isDefault programme { code } } }", self.member_a_token))["myProgrammes"]
        self.assertEqual({r["programme"]["code"]: r["isDefault"] for r in rows}, {"PGA": False, "PGE": True})

        internal_id = self._mutate(
            "setMyDefaultProgramme", f'programmeId: "{self.programme_b.id}"', self.member_a_token)
        self.assert_mutation_error(internal_id, self.member_a_token, NOT_A_MEMBER_MESSAGE)
