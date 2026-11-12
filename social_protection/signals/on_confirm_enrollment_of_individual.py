import logging
import uuid

from django.core.exceptions import ValidationError
from individual.models import (
    IndividualDataSourceUpload,
    IndividualDataSource
)
from social_protection.apps import SocialProtectionConfig
from social_protection.models import (
    Beneficiary,
    BenefitPlanDataUploadRecords,
    BenefitPlan
)
from social_protection.programme_scope import assert_benefit_plan_in_scope
from social_protection.utils import calculate_percentage_of_invalid_items
from tasks_management.models import Task
from tasks_management.apps import TasksManagementConfig
from tasks_management.services import (
    TaskService
)

logger = logging.getLogger(__name__)


def on_confirm_enrollment_of_individual(**kwargs):
    from core import datetime
    result = kwargs.get('result', None)
    benefit_plan_id = result['benefit_plan_id']
    status = result['status']
    user = result['user']
    assert_benefit_plan_in_scope(user, benefit_plan_id)
    individuals_to_upload = result['individuals_not_assigned_to_selected_programme']
    if SocialProtectionConfig.enable_maker_checker_logic_enrollment:
        benefit_plan = BenefitPlan.objects.get(id=benefit_plan_id)
        upload = IndividualDataSourceUpload(
            source_name=f"Enrollment into {benefit_plan.code} {datetime.date.today()}",
            source_type='beneficiary import'
        )
        upload.save(username=user.login_name)
        upload_record = BenefitPlanDataUploadRecords(
            data_upload=upload,
            benefit_plan_id=benefit_plan_id,
            workflow="Enrollment"
        )
        upload_record.save(username=user.username)
        data_source_objects = []
        for individual in individuals_to_upload:
            source = IndividualDataSource(
                uuid=uuid.uuid4(),
                user_created=user,
                user_updated=user,
                upload=upload,
                individual=individual,
                json_ext=individual.json_ext,
                validations={}
            )
            data_source_objects.append(source)
        IndividualDataSource.objects.bulk_create(data_source_objects)
        json_ext = {
            'source_name': upload_record.data_upload.source_name,
            'workflow': upload_record.workflow,
            'percentage_of_invalid_items': calculate_percentage_of_invalid_items(upload_record.id),
            'data_upload_id': str(upload.id),
            'benefit_plan_id': benefit_plan_id,
            'beneficiary_status': status
        }
        TaskService(user).create({
            'source': 'import_valid_items',
            'entity': upload_record,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': SocialProtectionConfig.validation_enrollment,
            'json_ext': json_ext
        })
    else:
        new_beneficiaries = []
        for individual in individuals_to_upload:
            # Create a new Beneficiary instance
            beneficiary = Beneficiary(
                individual=individual,
                benefit_plan_id=benefit_plan_id,
                status=status,
                json_ext=individual.json_ext,
                user_created=user,
                user_updated=user,
                uuid=uuid.uuid4(),
            )
            new_beneficiaries.append(beneficiary)
        try:
            Beneficiary.objects.bulk_create(new_beneficiaries)
        except ValidationError as e:
            logger.error(f"Validation error occurred: {e}")
