import re
import uuid
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from individual.models import IndividualDataSource, IndividualDataSourceUpload
from social_protection import views
from social_protection.models import (
    BenefitPlan,
    BenefitPlanDataUploadRecords,
    Beneficiary,
    BeneficiaryProjectEnrollment,
    BeneficiaryProjectTimeEntry,
    BeneficiaryStatus,
    GroupBeneficiary,
    GroupBeneficiaryProjectEnrollment,
    GroupBeneficiaryProjectTimeEntry,
    Project,
)
from social_protection.programme_scope import (
    NOT_A_MEMBER_MESSAGE,
    OUT_OF_SCOPE_MESSAGE,
    ProgrammeScopeError,
)
from social_protection.services import (
    BeneficiaryImportService,
    BeneficiaryService,
    BenefitPlanService,
    GroupBeneficiaryService,
    ProjectEnrollmentService,
    ProjectService,
)
from social_protection.gql_mutations import DeleteBenefitPlanMutation
from social_protection.signals.on_confirm_enrollment_of_group import on_confirm_enrollment_of_group
from social_protection.signals.on_confirm_enrollment_of_individual import on_confirm_enrollment_of_individual
from social_protection.signals.on_validation_import_valid_items import (
    _complete_task_for_accepted,
    _resolve_task_any,
    on_task_complete_action,
    on_task_resolve,
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
from social_protection.workflows.utils import BasePythonWorkflowExecutor

CHANGE_REQUIRES_SUPERUSER = "social_protection.programme.change_requires_superuser"
UPLOAD_NOT_IN_PLAN = "social_protection.validation.upload_not_in_benefit_plan"
SET_PROGRAMME_REQUIRED = "social_protection.programme.change_through_set_benefit_plan_programme"


class ProgrammeWriteScopeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        username = cls.admin.username
        cls.programme_a = create_programme("PWA", username)
        cls.programme_b = create_programme("PWB", username)
        cls.programme_c = create_programme("PWC", username)
        cls.member = create_user_with_rights("pwMemberAC")
        add_member(cls.member, cls.programme_a, username)
        add_member(cls.member, cls.programme_c, username)

        cls.plan_a = create_programme_benefit_plan("PWPA", cls.programme_a, username)
        cls.plan_b = create_programme_benefit_plan("PWPB", cls.programme_b, username)
        cls.plan_none = create_programme_benefit_plan("PWPN", None, username)
        cls.group_plan_a = create_programme_benefit_plan("PWGA", cls.programme_a, username, type="GROUP")
        cls.group_plan_b = create_programme_benefit_plan("PWGB", cls.programme_b, username, type="GROUP")

        admin_beneficiaries = BeneficiaryService(cls.admin)
        cls.individual = create_individual(username)
        cls.beneficiary_a_id = add_individual_to_benefit_plan(
            admin_beneficiaries, cls.individual, cls.plan_a, payload_override={"status": "ACTIVE"})
        cls.beneficiary_b_id = add_individual_to_benefit_plan(
            admin_beneficiaries, create_individual(username), cls.plan_b, payload_override={"status": "ACTIVE"})
        _, cls.group, _ = create_group_with_individual(username)
        cls.group_beneficiary_b_id = add_group_to_benefit_plan(
            GroupBeneficiaryService(cls.admin), cls.group, cls.group_plan_b, payload_override={"status": "ACTIVE"})
        _, group_a, _ = create_group_with_individual(username)
        cls.group_beneficiary_a_id = add_group_to_benefit_plan(
            GroupBeneficiaryService(cls.admin), group_a, cls.group_plan_a, payload_override={"status": "ACTIVE"})

        cls.project_a = create_project("PW project A", cls.plan_a, username)
        cls.project_b = create_project("PW project B", cls.plan_b, username)
        cls.enrollment_b = BeneficiaryProjectEnrollment(
            beneficiary_id=cls.beneficiary_b_id, project=cls.project_b)
        cls.enrollment_b.save(username=username)
        cls.enrollment_a = BeneficiaryProjectEnrollment(
            beneficiary_id=cls.beneficiary_a_id, project=cls.project_a)
        cls.enrollment_a.save(username=username)
        cls.time_entry_b = BeneficiaryProjectTimeEntry(
            enrollment=cls.enrollment_b, day_number=1, percent_complete=10)
        cls.time_entry_b.save(username=username)

        cls.group_project_a = create_project("PW group project A", cls.group_plan_a, username)
        cls.group_project_b = create_project("PW group project B", cls.group_plan_b, username)
        cls.group_enrollment_a = GroupBeneficiaryProjectEnrollment(
            group_beneficiary_id=cls.group_beneficiary_a_id, project=cls.group_project_a)
        cls.group_enrollment_a.save(username=username)
        cls.group_enrollment_b = GroupBeneficiaryProjectEnrollment(
            group_beneficiary_id=cls.group_beneficiary_b_id, project=cls.group_project_b)
        cls.group_enrollment_b.save(username=username)
        cls.group_time_entry_b = GroupBeneficiaryProjectTimeEntry(
            enrollment=cls.group_enrollment_b, day_number=1, percent_complete=10)
        cls.group_time_entry_b.save(username=username)

        cls.upload_b = IndividualDataSourceUpload(source_name="upload PWPB", source_type="beneficiary import")
        cls.upload_b.save(username=username)
        cls.upload_record_b = BenefitPlanDataUploadRecords(
            data_upload=cls.upload_b, benefit_plan=cls.plan_b, workflow="test")
        cls.upload_record_b.save(username=username)

    def assertRefused(self, result, message=OUT_OF_SCOPE_MESSAGE):
        self.assertFalse(result.get("success"), result)
        self.assertIn(message, result.get("detail", ""))

    def _plan_payload(self, plan, **override):
        return {
            "id": plan.id, "code": plan.code, "name": plan.name,
            "date_valid_from": "2024-01-01", "date_valid_to": "2030-12-31", **override,
        }

    # Benefit plans

    def test_benefit_plan_update_and_delete_are_refused_out_of_scope(self):
        service = BenefitPlanService(self.member)
        self.assertRefused(service.update(self._plan_payload(self.plan_b, name="changed")))
        self.assertRefused(service.create_update_task(self._plan_payload(self.plan_b, name="changed")))
        self.assertRefused(service.delete({"id": self.plan_b.id}))
        self.assertEqual(BenefitPlan.objects.get(id=self.plan_b.id).name, self.plan_b.name)
        self.assertFalse(BenefitPlan.objects.get(id=self.plan_b.id).is_deleted)

    def test_benefit_plan_undo_delete_and_close_are_refused_out_of_scope(self):
        BenefitPlan.objects.filter(id=self.plan_b.id).update(is_deleted=True)
        service = BenefitPlanService(self.member)
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            service.undo_delete({"id": self.plan_b.id})
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            service.close_benefit_plan({"id": self.plan_b.id})

    def test_benefit_plan_create_in_a_foreign_programme_is_refused(self):
        result = BenefitPlanService(self.member).create({
            "code": "PWNEW", "name": "PW new", "programme_id": self.programme_b.id,
            "date_valid_from": "2024-01-01", "date_valid_to": "2030-12-31",
        })
        self.assertRefused(result, NOT_A_MEMBER_MESSAGE)

    def test_benefit_plan_create_in_own_programme_succeeds(self):
        result = BenefitPlanService(self.member).create({
            "code": "PWOWN", "name": "PW own", "programme_id": self.programme_a.id,
            "date_valid_from": "2024-01-01", "date_valid_to": "2030-12-31",
        })
        self.assertTrue(result["success"], result)
        self.assertEqual(BenefitPlan.objects.get(code="PWOWN").programme_id, self.programme_a.id)

    def test_update_refuses_any_programme_change(self):
        for user in (self.member, self.admin):
            for plan, target in ((self.plan_a, self.programme_c.id), (self.plan_a, None),
                                 (self.plan_none, self.programme_a.id)):
                with self.subTest(user=user.username, plan=plan.code, target=target):
                    self.assertRefused(
                        BenefitPlanService(user).update(self._plan_payload(plan, programme_id=target)),
                        SET_PROGRAMME_REQUIRED)
                    self.assertEqual(BenefitPlan.objects.get(id=plan.id).programme_id, plan.programme_id)

    def test_superuser_sets_moves_and_clears_the_programme_through_the_service(self):
        service = BenefitPlanService(self.admin)
        for target in (self.programme_a.id, self.programme_b.id, None):
            with self.subTest(target=target):
                result = service.set_programme({"id": self.plan_none.id, "programme_id": target})
                self.assertTrue(result["success"], result)
                self.assertEqual(BenefitPlan.objects.get(id=self.plan_none.id).programme_id, target)

    def test_set_programme_is_refused_to_non_superusers(self):
        result = BenefitPlanService(self.member).set_programme(
            {"id": self.plan_a.id, "programme_id": self.programme_c.id})
        self.assertRefused(result, CHANGE_REQUIRES_SUPERUSER)
        self.assertEqual(BenefitPlan.objects.get(id=self.plan_a.id).programme_id, self.programme_a.id)

    def test_keeping_the_same_programme_on_update_is_allowed(self):
        result = BenefitPlanService(self.member).update(
            self._plan_payload(self.plan_a, programme_id=self.programme_a.id, name="same programme"))
        self.assertTrue(result["success"], result)

    def test_benefit_plan_update_in_scope_succeeds(self):
        result = BenefitPlanService(self.member).update(self._plan_payload(self.plan_none, name="renamed"))
        self.assertTrue(result["success"], result)

    # Beneficiaries

    def test_beneficiary_writes_are_refused_out_of_scope(self):
        service = BeneficiaryService(self.member)
        payload = {"individual_id": create_individual(self.admin.username).id,
                   "benefit_plan_id": self.plan_b.id, "status": "POTENTIAL", "json_ext": {}}
        self.assertRefused(service.create(payload))
        self.assertRefused(service.create_create_task(payload))
        update = {"id": self.beneficiary_b_id, "benefit_plan_id": self.plan_b.id, "status": "SUSPENDED"}
        self.assertRefused(service.update(update))
        self.assertRefused(service.create_update_task(update))
        self.assertRefused(service.delete({"id": self.beneficiary_b_id}))
        self.assertRefused(service.create_delete_task({"id": self.beneficiary_b_id}))
        self.assertEqual(Beneficiary.objects.get(id=self.beneficiary_b_id).status, BeneficiaryStatus.ACTIVE)

    def test_moving_a_beneficiary_into_an_out_of_scope_plan_is_refused(self):
        result = BeneficiaryService(self.member).update({
            "id": self.beneficiary_a_id, "benefit_plan_id": self.plan_b.id, "status": "ACTIVE",
        })
        self.assertRefused(result)
        self.assertEqual(Beneficiary.objects.get(id=self.beneficiary_a_id).benefit_plan_id, self.plan_a.id)

    def test_beneficiary_write_in_scope_succeeds(self):
        result = BeneficiaryService(self.member).update({
            "id": self.beneficiary_a_id, "benefit_plan_id": self.plan_a.id, "status": "SUSPENDED",
        })
        self.assertTrue(result["success"], result)

    def test_group_beneficiary_writes_are_refused_out_of_scope(self):
        service = GroupBeneficiaryService(self.member)
        _, group, _ = create_group_with_individual(self.admin.username)
        self.assertRefused(service.create({
            "group_id": group.id, "benefit_plan_id": self.group_plan_b.id, "status": "POTENTIAL", "json_ext": {},
        }))
        self.assertRefused(service.update({
            "id": self.group_beneficiary_b_id, "benefit_plan_id": self.group_plan_b.id, "status": "SUSPENDED",
        }))
        self.assertRefused(service.delete({"id": self.group_beneficiary_b_id}))
        self.assertFalse(GroupBeneficiary.objects.get(id=self.group_beneficiary_b_id).is_deleted)

    # Projects

    def test_project_writes_are_refused_out_of_scope(self):
        service = ProjectService(self.member)
        self.assertRefused(service.create({
            "benefit_plan": self.plan_b, "name": "PW new project", "activity": self.project_a.activity,
            "location": self.project_a.location, "target_beneficiaries": 1, "working_days": 1,
        }))
        self.assertRefused(service.update({"id": self.project_b.id, "name": "renamed"}))
        self.assertRefused(service.update({"id": self.project_a.id, "benefit_plan": self.plan_b}))
        self.assertRefused(service.delete({"id": self.project_b.id}))
        Project.objects.filter(id=self.project_b.id).update(is_deleted=True)
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            service.undo_delete({"id": self.project_b.id})
        self.assertEqual(Project.objects.get(id=self.project_a.id).benefit_plan_id, self.plan_a.id)

    def test_group_enrollment_is_refused_out_of_scope(self):
        service = ProjectEnrollmentService(self.member, ProjectEnrollmentService.GROUP)
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            service.enroll_project({"project_id": self.group_project_b.id, "ids": [self.group_beneficiary_b_id]})
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            service.enroll_project({"project_id": self.group_project_a.id, "ids": [self.group_beneficiary_b_id]})

    def test_time_entry_of_another_enrollment_cannot_be_overwritten(self):
        for enrollment_type, own_enrollment, foreign_entry, model in (
            (ProjectEnrollmentService.INDIVIDUAL, self.enrollment_a, self.time_entry_b, BeneficiaryProjectTimeEntry),
            (ProjectEnrollmentService.GROUP, self.group_enrollment_a, self.group_time_entry_b,
             GroupBeneficiaryProjectTimeEntry),
        ):
            with self.subTest(enrollment_type=enrollment_type):
                service = ProjectEnrollmentService(self.member, enrollment_type)
                with self.assertRaises((ValueError, ProgrammeScopeError)):
                    service.bulk_update_time_entries({"time_entries": [{
                        "id": foreign_entry.id, "enrollment_id": own_enrollment.id,
                        "day_number": 2, "percent_complete": 99,
                    }]})
                entry = model.objects.get(id=foreign_entry.id)
                self.assertEqual(entry.enrollment_id, foreign_entry.enrollment_id)
                self.assertEqual(entry.percent_complete, 10)

    def test_enrollment_and_time_entries_are_refused_out_of_scope(self):
        service = ProjectEnrollmentService(self.member, ProjectEnrollmentService.INDIVIDUAL)
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            service.enroll_project({"project_id": self.project_b.id, "ids": [self.beneficiary_b_id]})
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            service.enroll_project({"project_id": self.project_a.id, "ids": [self.beneficiary_b_id]})
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            service.bulk_update_time_entries({"time_entries": [{
                "enrollment_id": self.enrollment_b.id, "day_number": 1, "percent_complete": 50,
            }]})

    # Imports, tasks, workflows

    def test_import_service_entry_points_are_refused_out_of_scope(self):
        service = BeneficiaryImportService(self.member)
        calls = [
            lambda: service.validate_import_beneficiaries(self.upload_b.id, [], self.plan_b),
            lambda: service.create_task_with_importing_valid_items(self.upload_b.id, self.plan_b),
            lambda: service.create_task_with_update_valid_items(self.upload_b.id, self.plan_b),
            lambda: service.synchronize_data_for_reporting(self.upload_b.id, self.plan_b.id),
            lambda: service.import_beneficiaries(mock.Mock(), self.plan_b, mock.Mock(), None),
        ]
        for call in calls:
            with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
                call()

    def test_task_completion_by_an_out_of_scope_user_fails_the_upload(self):
        from social_protection.apps import SocialProtectionConfig
        from tasks_management.models import Task
        workflow_run = mock.patch(
            "social_protection.signals.on_validation_import_valid_items."
            "IndividualItemsImportTaskCompletionEvent.run_workflow")
        with workflow_run as run_workflow:
            on_task_complete_action(
                SocialProtectionConfig.validation_import_valid_items,
                result={"success": True, "data": {
                    "task": {
                        "business_event": SocialProtectionConfig.validation_import_valid_items,
                        "status": Task.Status.COMPLETED,
                        "entity_id": self.upload_record_b.id,
                    },
                    "user": {"id": str(self.member.id)},
                }},
            )
        run_workflow.assert_not_called()
        upload = IndividualDataSourceUpload.objects.get(id=self.upload_b.id)
        self.assertEqual(upload.status, IndividualDataSourceUpload.Status.FAIL)
        self.assertIn(OUT_OF_SCOPE_MESSAGE, str(upload.error))

    def test_partial_task_resolution_by_an_out_of_scope_user_is_refused(self):
        task = mock.Mock(entity_id=self.upload_record_b.id, business_event="any")
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            _complete_task_for_accepted(task, [], self.member)

    def test_enrollment_confirmation_into_an_out_of_scope_plan_is_refused(self):
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            on_confirm_enrollment_of_individual(result={
                "benefit_plan_id": str(self.plan_b.id), "status": "POTENTIAL", "user": self.member,
                "individuals_not_assigned_to_selected_programme": [],
            })

    def test_group_enrollment_confirmation_into_an_out_of_scope_plan_is_refused(self):
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            on_confirm_enrollment_of_group(result={
                "benefit_plan_id": str(self.group_plan_b.id), "status": "POTENTIAL", "user": self.member,
                "groups_not_assigned_to_selected_programme": GroupBeneficiary.objects.none(),
            })

    def test_partial_resolution_checks_scope_before_deleting_rejected_items(self):
        source = IndividualDataSource(upload=self.upload_b, json_ext={"first_name": "x"}, validations={})
        source.save(username=self.admin.username)
        task = mock.Mock(
            entity_id=self.upload_record_b.id, source="import_valid_items", business_event="any",
            business_status={str(self.member.id): {"REJECT": [str(source.id)], "ACCEPT": []}},
        )
        task.history.first.return_value.prev_record = None
        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            _resolve_task_any(task, self.member)
        self.assertFalse(IndividualDataSource.objects.get(id=source.id).is_deleted)

    def test_close_task_completion_by_an_out_of_scope_user_leaves_the_plan_open(self):
        from social_protection.apps import SocialProtectionConfig
        from social_protection.signals import bind_service_signals
        from tasks_management.models import Task
        handlers = []
        with mock.patch("social_protection.signals.bind_service_signal",
                        side_effect=lambda name, handler, bind_type: handlers.append(handler)):
            bind_service_signals()
        close_handler = next(h for h in handlers if getattr(h, "__name__", "") == "on_task_close_benefit_plan")
        close_handler(result={"success": True, "data": {
            "task": {"business_event": SocialProtectionConfig.benefit_plan_suspend,
                     "status": Task.Status.COMPLETED, "entity_id": str(self.plan_b.id)},
            "user": {"id": str(self.member.id)},
        }})
        self.assertEqual(
            BenefitPlan.objects.get(id=self.plan_b.id).date_valid_to, self.plan_b.date_valid_to)

    def test_rejected_ids_of_another_upload_are_left_untouched(self):
        other_upload = IndividualDataSourceUpload(source_name="other upload", source_type="beneficiary import")
        other_upload.save(username=self.admin.username)
        foreign_source = IndividualDataSource(upload=other_upload, json_ext={"first_name": "y"}, validations={})
        foreign_source.save(username=self.admin.username)
        own_source = IndividualDataSource(upload=self.upload_b, json_ext={"first_name": "z"}, validations={})
        own_source.save(username=self.admin.username)
        task = mock.Mock(
            entity_id=self.upload_record_b.id, source="import_valid_items", business_event="any",
            business_status={str(self.admin.id): {
                "REJECT": [str(foreign_source.id), str(own_source.id)], "ACCEPT": [],
            }},
        )
        task.history.first.return_value.prev_record = None
        _resolve_task_any(task, self.admin)
        self.assertFalse(IndividualDataSource.objects.get(id=foreign_source.id).is_deleted)
        self.assertTrue(IndividualDataSource.objects.get(id=own_source.id).is_deleted)

    def _task_payload(self, task_id, business_event, entity_id, status):
        return {"success": True, "data": {
            "task": {"id": task_id, "business_event": business_event, "status": status,
                     "entity_id": str(entity_id), "executor_action_event": "APPROVED"},
            "user": {"id": str(self.member.id)},
        }}

    def _assert_logged(self, logs, task_id):
        self.assertTrue(
            any(task_id in line and OUT_OF_SCOPE_MESSAGE in line for line in logs.output), logs.output)

    def test_refused_import_task_completion_is_logged_with_the_task_id(self):
        from social_protection.apps import SocialProtectionConfig
        from tasks_management.models import Task
        task_id = str(uuid.uuid4())
        with self.assertLogs("social_protection.signals.on_validation_import_valid_items", level="ERROR") as logs:
            on_task_complete_action(
                SocialProtectionConfig.validation_import_valid_items,
                result=self._task_payload(task_id, SocialProtectionConfig.validation_import_valid_items,
                                          self.upload_record_b.id, Task.Status.COMPLETED),
            )
        self._assert_logged(logs, task_id)

    def test_refused_partial_resolution_is_logged_with_the_task_id(self):
        from social_protection.apps import SocialProtectionConfig
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        task_id = str(uuid.uuid4())
        task = mock.Mock(
            id=task_id, entity_id=self.upload_record_b.id, source="import_valid_items", business_event="any",
            business_status={}, task_group=mock.Mock(completion_policy="ANY"),
        )
        payload = self._task_payload(task_id, SocialProtectionConfig.validation_import_valid_items,
                                     self.upload_record_b.id, Task.Status.ACCEPTED)
        payload["data"]["task"]["executor_action_event"] = TasksManagementConfig.default_executor_event
        manager = mock.Mock()
        manager.select_related.return_value.prefetch_related.return_value.get.return_value = task
        with mock.patch.object(Task, "objects", manager), \
                self.assertLogs("social_protection.signals.on_validation_import_valid_items", level="ERROR") as logs:
            on_task_resolve(result=payload)
        self._assert_logged(logs, task_id)

    def test_refused_close_task_is_logged_with_the_task_id(self):
        from social_protection.apps import SocialProtectionConfig
        from social_protection.signals import bind_service_signals
        from tasks_management.models import Task
        handlers = []
        with mock.patch("social_protection.signals.bind_service_signal",
                        side_effect=lambda name, handler, bind_type: handlers.append(handler)):
            bind_service_signals()
        close_handler = next(h for h in handlers if getattr(h, "__name__", "") == "on_task_close_benefit_plan")
        task_id = str(uuid.uuid4())
        with self.assertLogs("social_protection.signals", level="ERROR") as logs:
            close_handler(result=self._task_payload(
                task_id, SocialProtectionConfig.benefit_plan_suspend, self.plan_b.id, Task.Status.COMPLETED))
        self._assert_logged(logs, task_id)

    def test_python_workflow_refuses_an_out_of_scope_user(self):
        class _Executor(BasePythonWorkflowExecutor):
            def execute(self, *args, **kwargs):
                pass

        with self.assertRaisesMessage(ProgrammeScopeError, OUT_OF_SCOPE_MESSAGE):
            _Executor(self.plan_b.id, self.upload_b.id, self.member.id)


class ProgrammeRestScopeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        username = cls.admin.username
        cls.programme_a = create_programme("PRA", username)
        cls.programme_b = create_programme("PRB", username)
        cls.member = create_user_with_rights("prMemberA")
        add_member(cls.member, cls.programme_a, username)
        cls.plan_a = create_programme_benefit_plan("PRPA", cls.programme_a, username)
        cls.plan_b = create_programme_benefit_plan("PRPB", cls.programme_b, username)
        cls.upload_b = IndividualDataSourceUpload(source_name="upload PRPB", source_type="beneficiary import")
        cls.upload_b.save(username=username)
        BenefitPlanDataUploadRecords(data_upload=cls.upload_b, benefit_plan=cls.plan_b, workflow="test").save(
            username=username)
        cls.upload_a = IndividualDataSourceUpload(source_name="upload PRPA", source_type="beneficiary import")
        cls.upload_a.save(username=username)
        BenefitPlanDataUploadRecords(data_upload=cls.upload_a, benefit_plan=cls.plan_a, workflow="test").save(
            username=username)
        cls.unlinked_upload = IndividualDataSourceUpload(source_name="unlinked", source_type="beneficiary import")
        cls.unlinked_upload.save(username=username)
        cls.source_b = IndividualDataSource(upload=cls.upload_b, json_ext={"first_name": "x"}, validations={})
        cls.source_b.save(username=username)

    def _get(self, view, params, **headers):
        request = APIRequestFactory().get("/", params, **headers)
        force_authenticate(request, user=self.member)
        return view(request)

    def _post(self, view, data):
        request = APIRequestFactory().post("/", data, format="json")
        force_authenticate(request, user=self.member)
        return view(request)

    def test_template_download_is_scoped(self):
        response = self._get(views.download_template_benefit_plan_file, {"benefit_plan_uuid": str(self.plan_b.id)})
        self.assertEqual(response.status_code, 403)
        response = self._get(views.download_template_benefit_plan_file, {"benefit_plan_uuid": str(self.plan_a.id)})
        self.assertEqual(response.status_code, 200)

    def test_rest_header_narrows_and_invalid_header_is_refused(self):
        response = self._get(
            views.download_template_benefit_plan_file, {"benefit_plan_uuid": str(self.plan_a.id)},
            HTTP_X_PROGRAMME=str(self.programme_b.id),
        )
        self.assertEqual(response.status_code, 403)

    def test_upload_file_and_invalid_items_downloads_are_scoped(self):
        response = self._get(views.download_beneficiary_upload,
                             {"benefit_plan_id": str(self.plan_b.id), "filename": "x.csv"})
        self.assertEqual(response.status_code, 403)
        response = self._get(views.download_invalid_items, {"upload_id": str(self.upload_b.id)})
        self.assertEqual(response.status_code, 403)

    def test_beneficiary_import_upload_is_scoped(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        request = APIRequestFactory().post("/", {
            "file": SimpleUploadedFile("import.csv", b"first_name\nA\n", content_type="text/csv"),
            "benefit_plan": str(self.plan_b.id),
            "workflow_name": "any", "workflow_group": "any",
        }, format="multipart")
        force_authenticate(request, user=self.member)
        response = views.import_beneficiaries(request)
        self.assertEqual(response.status_code, 403)
        self.assertIn(OUT_OF_SCOPE_MESSAGE, response.data["error"])

    def test_import_endpoints_refuse_an_upload_of_another_plan(self):
        for view in (views.validate_import_beneficiaries,
                     views.create_task_with_importing_valid_items,
                     views.synchronize_data_for_reporting):
            for upload in (self.upload_b, self.unlinked_upload):
                with self.subTest(view=view.__name__, upload=upload.source_name):
                    response = self._post(view, {"benefit_plan": str(self.plan_a.id), "upload_id": str(upload.id)})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(UPLOAD_NOT_IN_PLAN, response.data["error"])
        self.assertEqual(IndividualDataSource.objects.get(id=self.source_b.id).validations, {})

    def test_upload_file_download_rejects_traversal_and_missing_plans(self):
        cases = (
            ({"benefit_plan_id": str(self.plan_a.id), "filename": "../x.csv"}, 400),
            ({"benefit_plan_id": str(self.plan_a.id), "filename": "sub/x.csv"}, 400),
            ({"benefit_plan_id": "../..", "filename": "x.csv"}, 404),
            ({"benefit_plan_id": str(uuid.uuid4()), "filename": "x.csv"}, 404),
        )
        for params, expected_status in cases:
            with self.subTest(params=params):
                with mock.patch.object(views, "DefaultStorageFileHandler") as handler:
                    response = self._get(views.download_beneficiary_upload, params)
                self.assertEqual(response.status_code, expected_status)
                handler.assert_not_called()

    def test_template_download_of_a_missing_plan_is_not_found(self):
        response = self._get(views.download_template_benefit_plan_file, {"benefit_plan_uuid": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 404)

    def test_import_endpoints_are_scoped(self):
        for view in (views.validate_import_beneficiaries,
                     views.create_task_with_importing_valid_items,
                     views.synchronize_data_for_reporting):
            with self.subTest(view=view.__name__):
                response = self._post(view, {"benefit_plan": str(self.plan_b.id), "upload_id": str(uuid.uuid4())})
                self.assertEqual(response.status_code, 403)
                self.assertIn(OUT_OF_SCOPE_MESSAGE, response.data["error"])


def _scope_queries(captured):
    """Queries issued by the programme scope: programme tables or single-column plan lookups."""
    single_column = re.compile(r'^SELECT "social_protection_\w+"\."(programme_id|benefit_plan_id)" FROM')
    programme_tables = ("social_protection_programme", "social_protection_userprogramme")
    return [
        q["sql"] for q in captured.captured_queries
        if any(table in q["sql"] for table in programme_tables) or single_column.match(q["sql"])
    ]


class ZeroProgrammeWriteCostTest(TestCase):
    """Without any programme, writes pay at most one scope query per checked object."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        cls.member = create_user_with_rights("pzWriter")
        cls.plans = [create_programme_benefit_plan(code, None, cls.admin.username) for code in ("PZW1", "PZW2")]
        cls.beneficiary_id = add_individual_to_benefit_plan(
            BeneficiaryService(cls.admin), create_individual(cls.admin.username), cls.plans[0])

    def _update_scope_queries(self, user):
        with CaptureQueriesContext(connection) as captured:
            result = BeneficiaryService(user).update({
                "id": self.beneficiary_id, "benefit_plan_id": self.plans[0].id, "status": "SUSPENDED",
            })
        self.assertTrue(result["success"], result)
        return _scope_queries(captured)

    def _bulk_delete_scope_queries(self, user):
        with CaptureQueriesContext(connection) as captured:
            result = DeleteBenefitPlanMutation._mutate(user, ids=[plan.id for plan in self.plans])
        self.assertIsNone(result)
        return _scope_queries(captured)

    def test_beneficiary_update_costs_one_scope_query(self):
        self.assertEqual(len(self._update_scope_queries(self.member)), 1)

    def test_bulk_delete_costs_one_scope_query_per_id(self):
        self.assertEqual(len(self._bulk_delete_scope_queries(self.member)), len(self.plans))

    def test_superuser_writes_cost_no_scope_query(self):
        self.assertEqual(self._update_scope_queries(self.admin), [])
        self.assertEqual(self._bulk_delete_scope_queries(self.admin), [])
