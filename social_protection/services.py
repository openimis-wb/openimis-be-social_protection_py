import json
import logging
import uuid

import pandas as pd
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import transaction
from django.utils.translation import gettext as _
from pandas import DataFrame

from calculation.services import get_calculation_object
from core.services import BaseService
from core.signals import register_service_signal
from individual.models import (
    IndividualDataSourceUpload,
    IndividualDataSource,
    Individual,
)
from social_protection.apps import SocialProtectionConfig
from social_protection.models import (
    BenefitPlan,
    Beneficiary,
    BenefitPlanDataUploadRecords,
    GroupBeneficiary,
    Project,
    BeneficiaryProjectTimeEntry,
    GroupBeneficiaryProjectTimeEntry,
    BeneficiaryProjectEnrollment,
    GroupBeneficiaryProjectEnrollment,
    Programme,
    UserProgramme,
)
from social_protection.programme_scope import (
    assert_benefit_plan_in_scope,
    assert_benefit_plans_in_scope,
    forget_programme_scope,
    is_superuser,
    write_scope,
)
from social_protection.utils import (
    load_dataframe,
    fetch_summary_of_valid_items,
    fetch_summary_of_broken_items,
)
from social_protection.validation import (
    BeneficiaryValidation,
    BenefitPlanValidation,
    GroupBeneficiaryValidation,
    ProjectValidation,
    ProgrammeValidation,
)
from tasks_management.services import (
    UpdateCheckerLogicServiceMixin,
    CheckerLogicServiceMixin,
    crud_business_data_builder,
)
from workflow.systems.base import WorkflowHandler
from core.models import User
from core.services.utils import output_exception, output_result_success

logger = logging.getLogger(__name__)


class BenefitPlanService(BaseService, UpdateCheckerLogicServiceMixin):
    OBJECT_TYPE = BenefitPlan

    def __init__(self, user, validation_class=BenefitPlanValidation):
        super().__init__(user, validation_class)

    @register_service_signal('benefit_plan_service.create')
    def create(self, obj_data):
        result = super().create(obj_data)
        if obj_data.get('programme_id'):
            forget_programme_scope(self.user)
        return result

    @register_service_signal('benefit_plan_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('benefit_plan_service.delete')
    def delete(self, obj_data):
        obj_data = {k: v for k, v in obj_data.items() if k != 'user'}
        return super().delete(obj_data)

    @register_service_signal('benefit_plan_service.set_programme')
    def set_programme(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_set_programme(self.user, **obj_data)
                benefit_plan = BenefitPlan.objects.get(id=obj_data['id'])
                programme_id = obj_data.get('programme_id')
                if str(benefit_plan.programme_id) != str(programme_id):
                    benefit_plan.programme_id = programme_id
                    benefit_plan.save(user=self.user)
                    forget_programme_scope(self.user)
                return output_result_success({'id': str(benefit_plan.id), 'programme_id': str(programme_id)})
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="set_programme",
                exception=exc
            )

    @register_service_signal('benefit_plan_service.undo_delete')
    def undo_delete(self, obj_data):
        self.validation_class.validate_undo_delete(obj_data)
        obj_ = self.OBJECT_TYPE.objects.filter(
            id=obj_data['id']
        ).first()
        assert_benefit_plan_in_scope(self.user, obj_)
        obj_.is_deleted = False
        obj_.save(user=self.user)
        return {
            "success": True,
            "message": "Ok",
            "detail": "",
        }

    @register_service_signal('benefit_plan_service.close')
    def close_benefit_plan(self, obj_data):
        from tasks_management.models import Task
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.services import (
            _get_std_task_data_payload,
            TaskService,
        )
        from social_protection.apps import SocialProtectionConfig as SPConfig
        benefit_plan = BenefitPlan.objects.filter(
            id=obj_data.get('id')
        ).first()
        assert_benefit_plan_in_scope(self.user, benefit_plan)
        data = {'benefit_plan_id': benefit_plan.id}
        TaskService(self.user).create({
            'source': 'BenefitPlanService',
            'entity': benefit_plan,
            'status': Task.Status.RECEIVED,
            'executor_action_event': (
                TasksManagementConfig.default_executor_event
            ),
            'business_event': SPConfig.benefit_plan_suspend,
            'data': _get_std_task_data_payload(data)
        })


class BeneficiaryService(BaseService, CheckerLogicServiceMixin):
    OBJECT_TYPE = Beneficiary

    def __init__(self, user, validation_class=BeneficiaryValidation):
        super().__init__(user, validation_class)

    def would_exceed_max_active_beneficiaries(
        self, benefit_plan_id, status, id=None
    ):
        benefit_plan = BenefitPlan.objects.get(id=benefit_plan_id)
        if benefit_plan and status == "ACTIVE":
            max_active_beneficiaries = benefit_plan.max_beneficiaries
            active_beneficiaries = Beneficiary.objects.filter(
                is_deleted=False,
                benefit_plan_id=benefit_plan_id,
                status="ACTIVE"
            ).distinct()
            beneficiary_already_active = (
                active_beneficiaries.filter(id=id).exists() if id else False
            )
            return (
                active_beneficiaries.count() == max_active_beneficiaries
                and not beneficiary_already_active
            )
        return False

    @register_service_signal('beneficiary_service.create')
    def create(self, obj_data):
        try:
            status = obj_data.get("status", None)
            benefit_plan_id = obj_data.get("benefit_plan_id", None)

            if self.would_exceed_max_active_beneficiaries(
                benefit_plan_id, status
            ):
                raise ValueError(
                    "Error creating beneficiary with active status. "
                    "Benefit plan is already at max active beneficiaries"
                )
            return super().create(obj_data)
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="update",
                exception=exc
            )

    @register_service_signal('beneficiary_service.update')
    def update(self, obj_data):
        try:
            status = obj_data.get("status", None)
            benefit_plan_id = obj_data.get("benefit_plan_id", None)
            id = obj_data.get('id', None)

            if self.would_exceed_max_active_beneficiaries(
                benefit_plan_id, status, id
            ):
                raise ValueError(
                    "Error changing beneficiary to active status. "
                    "Benefit plan is already at max active beneficiaries"
                )
            return super().update(obj_data)
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="update",
                exception=exc
            )

    @register_service_signal('beneficiary_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)

    def _business_data_serializer(self, data):
        def serialize(key, value):
            if key == 'id':
                beneficiary = Beneficiary.objects.get(id=value)
                individual = beneficiary.individual
                return f'{individual.first_name} {individual.last_name}'
            elif key == 'benefit_plan_id':
                benefit_plan = BenefitPlan.objects.get(id=value)
                return benefit_plan.__str__()
            elif key == 'individual_id':
                individual = Individual.objects.get(id=value)
                return f'{individual.first_name} {individual.last_name}'
            else:
                return value

        serialized_data = crud_business_data_builder(data, serialize)
        return serialized_data


class GroupBeneficiaryService(BaseService, CheckerLogicServiceMixin):
    OBJECT_TYPE = GroupBeneficiary

    def __init__(self, user, validation_class=GroupBeneficiaryValidation):
        super().__init__(user, validation_class)

    def would_exceed_max_active_beneficiaries(
        self, benefit_plan_id, status, id=None
    ):
        benefit_plan = BenefitPlan.objects.get(id=benefit_plan_id)
        if benefit_plan and status == "ACTIVE":
            max_active_beneficiaries = benefit_plan.max_beneficiaries
            active_beneficiaries = GroupBeneficiary.objects.filter(
                is_deleted=False,
                benefit_plan_id=benefit_plan_id,
                status="ACTIVE"
            ).distinct()
            beneficiary_already_active = (
                active_beneficiaries.filter(id=id).exists() if id else False
            )
            return (
                active_beneficiaries.count() == max_active_beneficiaries
                and not beneficiary_already_active
            )
        return False

    @register_service_signal('group_beneficiary_service.create')
    def create(self, obj_data):
        try:
            status = obj_data.get("status", None)
            benefit_plan_id = obj_data.get("benefit_plan_id", None)

            if self.would_exceed_max_active_beneficiaries(
                benefit_plan_id, status
            ):
                raise ValueError(
                    "Error creating beneficiary with active status. "
                    "Benefit plan is already at max active beneficiaries"
                )
            return super().create(obj_data)
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="update",
                exception=exc
            )

    @register_service_signal('group_beneficiary_service.update')
    def update(self, obj_data):
        try:
            status = obj_data.get("status", None)
            benefit_plan_id = obj_data.get("benefit_plan_id", None)
            id = obj_data.get('id', None)

            if self.would_exceed_max_active_beneficiaries(
                benefit_plan_id, status, id
            ):
                raise ValueError(
                    "Error changing beneficiary to active status. "
                    "Benefit plan is already at max active beneficiaries"
                )
            return super().update(obj_data)
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="update",
                exception=exc
            )

    @register_service_signal('group_beneficiary_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)


class ProjectEnrollmentService:
    INDIVIDUAL = 'INDIVIDUAL'
    GROUP = 'GROUP'

    CONFIGS = {
        INDIVIDUAL: {
            'enrollment_model': BeneficiaryProjectEnrollment,
            'time_entry_model': BeneficiaryProjectTimeEntry,
            'fk_field': 'beneficiary_id',
            'error_label': 'Beneficiaries',
        },
        GROUP: {
            'enrollment_model': GroupBeneficiaryProjectEnrollment,
            'time_entry_model': GroupBeneficiaryProjectTimeEntry,
            'fk_field': 'group_beneficiary_id',
            'error_label': 'Group beneficiaries',
        },
    }

    def __init__(self, user, enrollment_type):
        self.user = user
        self.enrollment_type = enrollment_type
        self.config = self.CONFIGS[enrollment_type]

    @register_service_signal('project_enrollment_service.enroll_project')
    def enroll_project(self, obj_data):
        project_id = obj_data['project_id']
        beneficiary_ids = {
            uuid.UUID(str(bid)) for bid in obj_data.get('ids', [])
        }

        project = Project.objects.get(id=project_id)
        enrollment_model = self.config['enrollment_model']
        fk_field = self.config['fk_field']
        if write_scope(self.user) is not None:
            beneficiary_model = enrollment_model._meta.get_field(
                fk_field[:-len('_id')]
            ).related_model
            assert_benefit_plans_in_scope(self.user, {
                project.benefit_plan_id,
                *beneficiary_model.objects.filter(
                    id__in=beneficiary_ids
                ).values_list('benefit_plan_id', flat=True),
            })

        # including deleted
        all_enrollments = {
            getattr(e, fk_field): e
            for e in enrollment_model.objects.filter(project_id=project_id)
        }

        currently_enrolled = {
            bid for bid, e in all_enrollments.items() if not e.is_deleted
        }

        to_enroll = beneficiary_ids - currently_enrolled
        to_unenroll = currently_enrolled - beneficiary_ids

        if not project.allows_multiple_enrollments and to_enroll:
            already_enrolled_elsewhere = set(
                enrollment_model.objects.filter(
                    **{f'{fk_field}__in': to_enroll},
                    is_deleted=False
                ).exclude(
                    project_id=project_id
                ).values_list(fk_field, flat=True)
            )
            if already_enrolled_elsewhere:
                msg = _(
                    "%(label)s %(ids)s are already enrolled in another "
                    "project. This project does not allow multiple "
                    "enrollments."
                ) % {
                    'label': self.config['error_label'],
                    'ids': already_enrolled_elsewhere
                }
                raise ValueError(msg)

        data_list = []

        for beneficiary_id in to_unenroll:
            enrollment = all_enrollments[beneficiary_id]
            data_list.append({'id': enrollment.id, 'is_deleted': True})

        for beneficiary_id in to_enroll:
            if beneficiary_id in all_enrollments:
                enrollment = all_enrollments[beneficiary_id]
                data_list.append({'id': enrollment.id, 'is_deleted': False})
            else:
                data_list.append(
                    {fk_field: beneficiary_id, 'project_id': project_id}
                )

        enrollment_model.bulk_save(
            data_list, self.user, include_deleted=True
        )

    @register_service_signal(
        'project_enrollment_service.bulk_update_time_entries'
    )
    def bulk_update_time_entries(self, obj_data):
        time_entries_data = obj_data.get('time_entries', [])

        if not time_entries_data:
            return

        enrollment_model = self.config['enrollment_model']
        time_entry_model = self.config['time_entry_model']

        enrollment_ids = {str(e['enrollment_id']) for e in time_entries_data}
        enrollments = enrollment_model.objects.filter(
            id__in=enrollment_ids,
            is_deleted=False
        ).select_related('project')

        enrollment_map = {str(e.id): e for e in enrollments}
        valid_enrollment_ids = set(enrollment_map.keys())

        if invalid_ids := enrollment_ids - valid_enrollment_ids:
            raise ValueError(
                _('Invalid enrollment IDs: %(ids)s') % {'ids': invalid_ids}
            )
        self._assert_entries_belong_to_their_enrollments(
            time_entry_model, time_entries_data
        )
        if write_scope(self.user) is not None:
            beneficiary_field = self.config['fk_field'][:-len('_id')]
            assert_benefit_plans_in_scope(self.user, {
                benefit_plan_id
                for pair in enrollment_model.objects.filter(id__in=valid_enrollment_ids).values_list(
                    'project__benefit_plan_id', f'{beneficiary_field}__benefit_plan_id'
                )
                for benefit_plan_id in pair
            })

        for entry in time_entries_data:
            enrollment = enrollment_map[str(entry['enrollment_id'])]
            project = enrollment.project

            day = entry['day_number']
            if not 1 <= day <= project.working_days:
                raise ValidationError(
                    _('Day number must be between 1 and %(working_days)s.')
                    % {'working_days': project.working_days}
                )

            percent = entry['percent_complete']
            if not 0 <= percent <= 100:
                raise ValidationError(
                    _('Percent complete must be between 0 and 100.')
                )

        time_entry_model.bulk_save(
            data_list=time_entries_data,
            user=self.user
        )

    @staticmethod
    def _assert_entries_belong_to_their_enrollments(time_entry_model, time_entries_data):
        """An entry given with an existing id keeps its enrollment: it is updated only through that enrollment."""
        requested = {
            str(entry['id']): str(entry['enrollment_id'])
            for entry in time_entries_data if entry.get('id')
        }
        if not requested:
            return
        existing = time_entry_model.objects.filter(
            id__in=requested.keys()
        ).values_list('id', 'enrollment_id')
        foreign = {
            str(entry_id) for entry_id, enrollment_id in existing
            if str(enrollment_id) != requested[str(entry_id)]
        }
        if foreign:
            raise ValueError(
                _('Time entries %(ids)s belong to another enrollment')
                % {'ids': sorted(foreign)}
            )


UPLOAD_NOT_IN_BENEFIT_PLAN_MESSAGE = "social_protection.validation.upload_not_in_benefit_plan"


def assert_upload_belongs_to_benefit_plan(upload_id, benefit_plan):
    """Refuses an upload that no live upload record links to `benefit_plan` (instance or id)."""
    benefit_plan_id = getattr(benefit_plan, 'id', benefit_plan)
    try:
        linked = BenefitPlanDataUploadRecords.objects.filter(
            data_upload_id=upload_id,
            benefit_plan_id=benefit_plan_id,
            is_deleted=False,
        ).exists()
    except (ValidationError, ValueError):
        linked = False
    if not linked:
        raise ValueError(UPLOAD_NOT_IN_BENEFIT_PLAN_MESSAGE)


class BeneficiaryImportService:
    import_loaders = {
        'text/csv': lambda f: pd.read_csv(f),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': (
            lambda f: pd.read_excel(f)
        ),
        'application/vnd.ms-excel': lambda f: pd.read_excel(f),
        'application/vnd.oasis.opendocument.spreadsheet': (
            lambda f: pd.read_excel(f)
        ),
    }

    def __init__(self, user):
        super().__init__()
        self.user = user

    @register_service_signal('benefit_plan.import_beneficiaries')
    def import_beneficiaries(
        self,
        import_file: InMemoryUploadedFile,
        benefit_plan: BenefitPlan,
        workflow: WorkflowHandler,
        group_aggregation_column: str
    ):
        assert_benefit_plan_in_scope(self.user, benefit_plan)
        upload = self._save_sources(import_file)
        self._create_benefit_plan_data_upload_records(
            benefit_plan, workflow, upload, group_aggregation_column
        )
        self._trigger_workflow(workflow, upload, benefit_plan)
        return {'success': True, 'data': {'upload_uuid': upload.uuid}}

    @transaction.atomic
    def _save_sources(self, import_file):
        # Method separated as workflow execution must be independent of
        # the atomic transaction.
        upload = self._create_upload_entry(import_file.name)
        dataframe = self._load_import_file(import_file)
        self._validate_dataframe(dataframe)
        self._save_data_source(dataframe, upload)
        return upload

    @transaction.atomic
    def _create_benefit_plan_data_upload_records(
        self, benefit_plan, workflow, upload, group_aggregation_column
    ):
        record = BenefitPlanDataUploadRecords(
            data_upload=upload,
            benefit_plan=benefit_plan,
            workflow=workflow.name,
            json_ext={"group_aggregation_column": group_aggregation_column}
        )
        record.save(user=self.user)

    def validate_import_beneficiaries(
        self, upload_id: uuid, individual_sources, benefit_plan: BenefitPlan
    ):
        assert_benefit_plan_in_scope(self.user, benefit_plan)
        assert_upload_belongs_to_benefit_plan(upload_id, benefit_plan)
        dataframe = self._load_dataframe(individual_sources)
        validated_dataframe, invalid_items = (
            self._validate_possible_beneficiaries(
                dataframe,
                benefit_plan,
                upload_id
            )
        )
        return {
            'success': True,
            'data': validated_dataframe,
            'summary_invalid_items': invalid_items
        }

    def create_task_with_importing_valid_items(
        self, upload_id: uuid, benefit_plan: BenefitPlan
    ):
        assert_benefit_plan_in_scope(self.user, benefit_plan)
        assert_upload_belongs_to_benefit_plan(upload_id, benefit_plan)
        if SocialProtectionConfig.enable_maker_checker_for_beneficiary_upload:
            BeneficiaryTaskCreatorService(self.user) \
                .create_task_with_importing_valid_items(
                    upload_id, benefit_plan)
        else:
            record = BenefitPlanDataUploadRecords.objects.get(
                data_upload_id=upload_id,
                is_deleted=False
            )
            from social_protection.signals.on_validation_import_valid_items \
                import IndividualItemsImportTaskCompletionEvent
            workflow = (
                SocialProtectionConfig.validation_import_valid_items_workflow
            )
            IndividualItemsImportTaskCompletionEvent(
                workflow,
                record,
                record.data_upload.id,
                record.benefit_plan,
                self.user
            ).run_workflow()

    def create_task_with_update_valid_items(
        self, upload_id: uuid, benefit_plan: BenefitPlan
    ):
        assert_benefit_plan_in_scope(self.user, benefit_plan)
        assert_upload_belongs_to_benefit_plan(upload_id, benefit_plan)
        mc_enabled = (
            SocialProtectionConfig.enable_maker_checker_for_beneficiary_update
        )
        if mc_enabled:
            BeneficiaryTaskCreatorService(self.user)\
                .create_task_with_update_valid_items(
                    upload_id, benefit_plan)

        if not mc_enabled:
            from social_protection.signals.on_validation_import_valid_items \
                import IndividualItemsUploadTaskCompletionEvent
            record = BenefitPlanDataUploadRecords.objects.get(
                data_upload_id=upload_id,
                benefit_plan=benefit_plan,
                is_deleted=False
            )
            workflow = (
                SocialProtectionConfig.validation_upload_valid_items_workflow
            )
            IndividualItemsUploadTaskCompletionEvent(
                workflow,
                record,
                record.data_upload.id,
                record.benefit_plan,
                self.user
            ).run_workflow()

    def synchronize_data_for_reporting(
        self, upload_id: uuid, benefit_plan: BenefitPlan
    ):
        assert_benefit_plan_in_scope(self.user, benefit_plan)
        assert_upload_belongs_to_benefit_plan(upload_id, benefit_plan)
        self._synchronize_individual(upload_id)
        self._synchronize_beneficiary(benefit_plan, upload_id)

    def _validate_possible_beneficiaries(
        self, dataframe: DataFrame, benefit_plan: BenefitPlan, upload_id: uuid
    ):

        if isinstance(benefit_plan.beneficiary_data_schema, str):
            schema_dict = json.loads(benefit_plan.beneficiary_data_schema)
        else:
            schema_dict = benefit_plan.beneficiary_data_schema
        properties = schema_dict.get("properties", {})

        calculation_uuid = SocialProtectionConfig.validation_calculation_uuid
        calculation = get_calculation_object(calculation_uuid)

        unique_fields = [
            field for field, props in properties.items()
            if "uniqueness" in props
        ]
        unique_validations = {}
        if unique_fields:
            unique_validations = {
                field: dataframe[field].duplicated(keep=False)
                for field in unique_fields
            }

        # TODO: Use ProcessPoolExecutor after resolving django
        # dependency loading issue
        validated_dataframe = BeneficiaryImportService.process_chunk(
            dataframe,
            properties,
            unique_validations,
            calculation,
            calculation_uuid,
        )

        self.save_validation_error_in_data_source_bulk(validated_dataframe)
        invalid_items = fetch_summary_of_broken_items(upload_id)
        return validated_dataframe, invalid_items

    @staticmethod
    def process_chunk(
        chunk, properties, unique_validations, calculation, calculation_uuid
    ):
        validated_dataframe = []
        for row_idx, row in chunk.iterrows():
            field_validation = {'row': row.to_dict(), 'validations': {}}
            for field, field_properties in properties.items():

                if (
                    "validationCalculation" in field_properties
                    and field in row
                ):
                    validation_name = (
                        field_properties["validationCalculation"]["name"]
                    )
                    field_validation['validations'][field] = (
                        calculation.calculate_if_active_for_object(
                            validation_name,
                            calculation_uuid,
                            field_name=field,
                            field_value=row[field]
                        )
                    )

                if "uniqueness" in field_properties and field in row:
                    field_validation['validations'][f'{field}_uniqueness'] = {
                        'success': not unique_validations[field].loc[row.name]
                    }

            validated_dataframe.append(field_validation)

        return validated_dataframe

    def _handle_uniqueness(
        self, row, field, field_properties, benefit_plan, dataframe
    ):
        unique_class_validation = (
            SocialProtectionConfig.unique_class_validation
        )
        calculation_uuid = SocialProtectionConfig.validation_calculation_uuid
        calculation = get_calculation_object(calculation_uuid)
        result_row = calculation.calculate_if_active_for_object(
            unique_class_validation,
            calculation_uuid,
            field_name=field,
            field_value=row[field],
            benefit_plan=benefit_plan.id,
            incoming_data=dataframe
        )
        return result_row

    def _handle_validation_calculation(self, row, field, field_properties):
        validation_calculation = field_properties.get(
            "validationCalculation", {}
        ).get("name")
        if not validation_calculation:
            raise ValueError("Missing validation name")
        calculation_uuid = SocialProtectionConfig.validation_calculation_uuid
        calculation = get_calculation_object(calculation_uuid)
        result_row = calculation.calculate_if_active_for_object(
            validation_calculation,
            calculation_uuid,
            field_name=field,
            field_value=row[field]
        )
        return result_row

    def _create_upload_entry(self, filename):
        upload = IndividualDataSourceUpload(
            source_name=filename, source_type='beneficiary import'
        )
        upload.save(username=self.user.login_name)
        return upload

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        if dataframe is None:
            raise ValueError("Unknown error while loading import file")
        if dataframe.empty:
            raise ValueError("Import file is empty")

    def _load_import_file(self, import_file) -> pd.DataFrame:
        if import_file.content_type not in self.import_loaders:
            raise ValueError(
                "Unsupported content type: {}".format(import_file.content_type)
            )

        return self.import_loaders[import_file.content_type](import_file)

    def _save_data_source(
        self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload
    ):
        data_source_objects = []

        for idx, row in dataframe.iterrows():
            ds = IndividualDataSource(
                upload=upload,
                json_ext=json.loads(row.to_json()),
                validations={},
                user_created=self.user,
                user_updated=self.user,
                uuid=uuid.uuid4()
            )
            data_source_objects.append(ds)

        IndividualDataSource.objects.bulk_create(data_source_objects)

    def _save_row(self, row, upload):
        ds = IndividualDataSource(
            upload=upload, json_ext=json.loads(row.to_json()), validations={}
        )
        ds.save(username=self.user.login_name)

    def _load_dataframe(self, individual_sources) -> pd.DataFrame:
        return load_dataframe(individual_sources)

    def _trigger_workflow(
        self,
        workflow: WorkflowHandler,
        upload: IndividualDataSourceUpload,
        benefit_plan: BenefitPlan
    ):
        try:
            # Before the run in order to avoid racing conditions
            upload.status = IndividualDataSourceUpload.Status.TRIGGERED
            upload.save(username=self.user.login_name)

            result = workflow.run({
                'user_uuid': str(
                    User.objects.get(username=self.user.login_name).id
                ),
                'benefit_plan_uuid': str(benefit_plan.uuid),
                'upload_uuid': str(upload.uuid),
            })

            if (
                result
                and isinstance(result, dict)
                and result.get('success') is False
            ):
                raise ValueError(
                    result.get(
                        'message', 'Unexpected error during the workflow'
                    )
                )
        except ValueError as e:
            upload.status = IndividualDataSourceUpload.Status.FAIL
            upload.error = {'workflow': str(e)}
            upload.save(username=self.user.login_name)
            return upload

    def save_validation_error_in_data_source_bulk(self, validated_dataframe):
        data_sources_to_update = []

        for field_validation in validated_dataframe:
            row = field_validation['row']
            error_fields = []

            for key, value in field_validation['validations'].items():
                if not value.get('success', False):
                    error_fields.append({
                        "field_name": value.get('field_name'),
                        "note": value.get('note')
                    })

            data_sources_to_update.append(
                IndividualDataSource(
                    id=row['id'],
                    validations={'validation_errors': error_fields}
                )
            )

        if data_sources_to_update:
            # Batched: one UPDATE ... CASE over every row grows much faster than the
            # row count, and never finished for 100,000 rows.
            IndividualDataSource.objects.bulk_update(
                data_sources_to_update, ['validations'], batch_size=1000
            )

    def _synchronize_individual(self, upload_id):
        individuals_to_update = Individual.objects.filter(
            individualdatasource__upload=upload_id
        )
        for individual in individuals_to_update:
            synch_status = {
                'report_synch': 'true',
                'version': individual.version + 1,
            }
            if individual.json_ext:
                individual.json_ext.update(synch_status)
            else:
                individual.json_ext = synch_status
            individual.save(user=self.user)

    def _synchronize_beneficiary(self, benefit_plan, upload_id):
        unique_uuids = list((
            Beneficiary.objects
            .filter(
                benefit_plan=benefit_plan,
                individual__individualdatasource__upload_id=upload_id
            )
            .values_list('id', flat=True)
            .distinct()
        ))
        beneficiaries = Beneficiary.objects.filter(
            id__in=unique_uuids
        )
        for beneficiary in beneficiaries:
            synch_status = {
                'report_synch': 'true',
                'version': beneficiary.version + 1,
            }
            if beneficiary.json_ext:
                beneficiary.json_ext.update(synch_status)
            else:
                beneficiary.json_ext = synch_status
            beneficiary.save(user=self.user)


class BeneficiaryTaskCreatorService:

    def __init__(self, user):
        self.user = user

    def create_task_with_importing_valid_items(
        self, upload_id: uuid, benefit_plan: BenefitPlan
    ):
        self._create_task(
            benefit_plan,
            upload_id,
            SocialProtectionConfig.validation_import_valid_items
        )

    def create_task_with_update_valid_items(
        self, upload_id: uuid, benefit_plan: BenefitPlan
    ):
        self._create_task(
            benefit_plan,
            upload_id,
            SocialProtectionConfig.validation_upload_valid_items
        )

    @register_service_signal('socialProtection.update_task')
    @transaction.atomic()
    def _create_task(self, benefit_plan, upload_id, business_event):
        from tasks_management.services import TaskService
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        upload_record = BenefitPlanDataUploadRecords.objects.get(
            data_upload_id=upload_id,
            benefit_plan=benefit_plan,
            is_deleted=False
        )
        json_ext = {
            'benefit_plan_code': benefit_plan.code,
            'source_name': upload_record.data_upload.source_name,
            'workflow': upload_record.workflow,
            'percentage_of_invalid_items': (
                self.__calculate_percentage_of_invalid_items(upload_id)
            ),
            'data_upload_id': str(upload_id)
        }
        TaskService(self.user).create({
            'source': 'import_valid_items',
            'entity': upload_record,
            'status': Task.Status.RECEIVED,
            'executor_action_event': (
                TasksManagementConfig.default_executor_event
            ),
            'business_event': business_event,
            'json_ext': json_ext
        })

        data_upload = upload_record.data_upload
        status = IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION
        data_upload.status = status
        data_upload.save(user=self.user)

    def __calculate_percentage_of_invalid_items(self, upload_id):
        number_of_valid_items = len(fetch_summary_of_valid_items(upload_id))
        number_of_invalid_items = len(fetch_summary_of_broken_items(upload_id))
        total_items = number_of_invalid_items + number_of_valid_items

        if total_items == 0:
            percentage_of_invalid_items = 0
        else:
            percentage_of_invalid_items = (
                (number_of_invalid_items / total_items) * 100
            )

        percentage_of_invalid_items = round(percentage_of_invalid_items, 2)
        return percentage_of_invalid_items


class GroupBeneficiaryImportService(BeneficiaryImportService):
    pass
    # TODO: create workflow upload/update groups and use it here


class ProjectService(BaseService):
    OBJECT_TYPE = Project

    def __init__(self, user, validation_class=ProjectValidation):
        super().__init__(user, validation_class)

    @register_service_signal("project_service.create")
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal("project_service.update")
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal("project_service.delete")
    def delete(self, obj_data):
        return super().delete(obj_data)

    @register_service_signal('project_service.undo_delete')
    def undo_delete(self, obj_data):
        self.validation_class.validate_undo_delete(obj_data)
        obj_ = self.OBJECT_TYPE.objects.filter(
            id=obj_data['id']
        ).first()
        assert_benefit_plan_in_scope(self.user, obj_.benefit_plan_id)
        obj_.is_deleted = False
        obj_.save(user=self.user)
        return {
            "success": True,
            "message": "Ok",
            "detail": "Undo Delete",
        }


class ProgrammeService(BaseService):
    OBJECT_TYPE = Programme

    def __init__(self, user, validation_class=ProgrammeValidation):
        super().__init__(user, validation_class)

    @register_service_signal('programme_service.create')
    def create(self, obj_data):
        """Creates the programme; a creator who is not a superuser becomes its member."""
        try:
            with transaction.atomic():
                result = super().create(obj_data)
                if result.get('success') and not is_superuser(self.user):
                    UserProgramme(
                        user_id=self.user.id,
                        programme_id=result['data']['id'],
                    ).save(user=self.user)
                    forget_programme_scope(self.user)
                return result
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="create",
                exception=exc
            )

    @register_service_signal('programme_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('programme_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)

    @register_service_signal('programme_service.add_members')
    def add_members(self, obj_data):
        try:
            with transaction.atomic():
                programme_id = obj_data.get('programme_id')
                self.validation_class.validate_members(self.user, programme_id)
                user_ids = self._resolve_user_ids(obj_data.get('user_ids'))
                already_members = set(UserProgramme.objects.filter(
                    programme_id=programme_id,
                    user_id__in=user_ids,
                    is_deleted=False,
                ).values_list('user_id', flat=True))
                added = user_ids - already_members
                for user_id in added:
                    UserProgramme(
                        user_id=user_id, programme_id=programme_id
                    ).save(user=self.user)
                forget_programme_scope(self.user)
                return output_result_success({'added': len(added)})
        except Exception as exc:
            return output_exception(
                model_name=UserProgramme.__name__,
                method="add_members",
                exception=exc
            )

    @register_service_signal('programme_service.remove_members')
    def remove_members(self, obj_data):
        try:
            with transaction.atomic():
                programme_id = obj_data.get('programme_id')
                self.validation_class.validate_members(self.user, programme_id)
                user_ids = self._resolve_user_ids(obj_data.get('user_ids'))
                memberships = list(UserProgramme.objects.filter(
                    programme_id=programme_id,
                    user_id__in=user_ids,
                    is_deleted=False,
                ))
                for membership in memberships:
                    membership.delete(user=self.user)
                forget_programme_scope(self.user)
                return output_result_success({'removed': len(memberships)})
        except Exception as exc:
            return output_exception(
                model_name=UserProgramme.__name__,
                method="remove_members",
                exception=exc
            )

    @register_service_signal('programme_service.set_default')
    def set_default(self, obj_data):
        """Makes `programme_id` the default programme of the acting user, who must be a member."""
        try:
            with transaction.atomic():
                programme_id = obj_data.get('programme_id')
                self.validation_class.validate_set_default(self.user, programme_id)
                memberships = UserProgramme.objects.filter(
                    user_id=self.user.id, is_deleted=False
                )
                for previous in memberships.filter(is_default=True).exclude(programme_id=programme_id):
                    previous.is_default = False
                    previous.save(user=self.user)
                membership = memberships.get(programme_id=programme_id)
                if not membership.is_default:
                    membership.is_default = True
                    membership.save(user=self.user)
                return output_result_success({'programme_id': str(programme_id)})
        except Exception as exc:
            return output_exception(
                model_name=UserProgramme.__name__,
                method="set_default",
                exception=exc
            )

    @staticmethod
    def _resolve_user_ids(user_ids):
        requested = {uuid.UUID(str(user_id)) for user_id in user_ids or []}
        if not requested:
            raise ValidationError(_("social_protection.programme.no_users"))
        found = set(User.objects.filter(id__in=requested).values_list('id', flat=True))
        if requested - found:
            raise ValidationError(
                _("social_protection.programme.unknown_users")
                + ": " + ", ".join(sorted(str(u) for u in requested - found))
            )
        return requested
