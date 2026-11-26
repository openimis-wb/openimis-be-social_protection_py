import logging

from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal
from core.models import User
from social_protection.apps import SocialProtectionConfig
from social_protection.services import BenefitPlanService, BeneficiaryService, GroupBeneficiaryService, GroupBeneficiary
from social_protection.models import BenefitPlan, Beneficiary, BeneficiaryStatus
from social_protection.programme_scope import assert_benefit_plan_in_scope
from social_protection.signals.on_validation_import_valid_items import on_task_complete_import_validated, \
    on_task_resolve

from social_protection.signals.on_confirm_enrollment_of_individual import on_confirm_enrollment_of_individual
from social_protection.signals.on_confirm_enrollment_of_group import on_confirm_enrollment_of_group

from tasks_management.models import Task
from tasks_management.services import on_task_complete_service_handler

logger = logging.getLogger(__name__)


def bind_service_signals():
    def on_task_close_benefit_plan(**kwargs):
        try:
            result = kwargs.get('result', None)
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            if result \
                    and result['success'] \
                    and task['business_event'] == SocialProtectionConfig.benefit_plan_suspend:
                task_status = task['status']
                if task_status == Task.Status.COMPLETED:
                    benefit_plan = BenefitPlan.objects.get(id=task['entity_id'])
                    assert_benefit_plan_in_scope(user, benefit_plan)
                    from core import datetime
                    now = datetime.datetime.now()
                    benefit_plan.date_valid_to = now
                    benefit_plan.save(username=user.username)
                    if benefit_plan.type == BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE:
                        beneficiaries = Beneficiary.objects.filter(benefit_plan=benefit_plan, is_deleted=False)
                        for beneficiary in beneficiaries:
                            if beneficiary.status != BeneficiaryStatus.GRADUATED:
                                beneficiary.status = BeneficiaryStatus.GRADUATED
                                beneficiary.save(username=user.username)
                    if benefit_plan.type == BenefitPlan.BenefitPlanType.GROUP_TYPE:
                        group_beneficiaries = GroupBeneficiary.objects.filter(benefit_plan=benefit_plan, is_deleted=False)
                        for group_beneficiary in group_beneficiaries:
                            if group_beneficiary.status != BeneficiaryStatus.GRADUATED:
                                group_beneficiary.status = BeneficiaryStatus.GRADUATED
                                group_beneficiary.save(username=user.username)
        except Exception as exc:
            task_id = (((kwargs.get('result') or {}).get('data') or {}).get('task') or {}).get('id')
            logger.error("Task %s did not close the benefit plan: %s", task_id, exc, exc_info=exc)

    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_service_handler(BenefitPlanService),
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_service_handler(BeneficiaryService),
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_service_handler(GroupBeneficiaryService),
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_import_validated,
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.resolve_task',
        on_task_resolve,
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'individual_service.select_individuals_to_benefit_plan',
        on_confirm_enrollment_of_individual,
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'group_service.select_groups_to_benefit_plan',
        on_confirm_enrollment_of_group,
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_close_benefit_plan,
        bind_type=ServiceSignalBindType.AFTER
    )
