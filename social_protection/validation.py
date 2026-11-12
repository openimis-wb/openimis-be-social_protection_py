from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from core.utils import validate_json_schema
from core.validation import BaseModelValidation, ObjectExistsValidationMixin
from social_protection.models import (
    Beneficiary, BenefitPlan, GroupBeneficiary, Programme, Project, UserProgramme,
)
from social_protection.programme_scope import (
    assert_benefit_plan_in_scope,
    assert_can_manage_programme,
    assert_can_set_programme,
)


class BenefitPlanValidation(BaseModelValidation, ObjectExistsValidationMixin):
    OBJECT_TYPE = BenefitPlan

    @classmethod
    def validate_create(cls, user, **data):
        errors = validate_benefit_plan(data)
        if errors:
            raise ValidationError(errors)
        if data.get('programme_id') is not None:
            assert_can_set_programme(user, data['programme_id'])
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        uuid = data.get('id')
        errors = validate_benefit_plan(data, uuid)
        if errors:
            raise ValidationError(errors)
        benefit_plan = BenefitPlan.objects.filter(id=uuid).first()
        if benefit_plan:
            assert_benefit_plan_in_scope(user, benefit_plan)
            if 'programme_id' in data:
                assert_can_set_programme(user, data['programme_id'], benefit_plan.programme_id)
        super().validate_update(user, **data)

    @classmethod
    def validate_delete(cls, user, **data):
        assert_benefit_plan_in_scope(user, data.get('id'))
        super().validate_delete(user, **data)

    @classmethod
    def validate_undo_delete(cls, data):
        obj_id = data.get('id')
        cls.validate_object_exists(obj_id)
        obj = BenefitPlan.objects.get(id=obj_id)
        errors = [
            *validate_bf_unique_code(obj.code, obj_id),
            *validate_bf_unique_name(obj.name, obj_id),
        ]
        if errors:
            raise ValidationError(errors)


def validate_benefit_plan(data, uuid=None):
    validations = [
        *validate_not_empty_field(data.get("code"), "code"),
        *validate_bf_unique_code(data.get('code'), uuid),
        *validate_not_empty_field(data.get("name"), "name"),
        *validate_bf_unique_name(data.get('name'), uuid)
    ]

    beneficiary_data_schema = data.get('beneficiary_data_schema')
    if beneficiary_data_schema:
        validations.extend(validate_json_schema(beneficiary_data_schema))

    return validations


def validate_bf_unique_code(code, uuid=None):
    instance = BenefitPlan.objects.filter(
        code=code, is_deleted=False
    ).exclude(id=uuid).first()
    if instance:
        msg = "social_protection.validation.benefit_plan.code_exists"
        return [{"message": _(msg % {'code': code})}]  # noqa: F504
    return []


def validate_bf_unique_name(name, uuid=None):
    instance = BenefitPlan.objects.filter(
        name=name, is_deleted=False
    ).exclude(id=uuid).first()
    if instance:
        msg = "social_protection.validation.benefit_plan.name_exists"
        return [{"message": _(msg % {'name': name})}]  # noqa: F504
    return []


def validate_not_empty_field(string, field):
    if not string:
        return [{"message": _("social_protection.validation.field_empty") % {
            'field': field
        }}]
    return []


class BenefitPlanScopeValidationMixin:
    """
    Refuses writes on rows of a benefit plan out of the user's programme scope:
    the row's current benefit plan and, when given, the target `benefit_plan_id`.
    """
    SCOPED_MODEL = None

    @classmethod
    def _assert_row_in_scope(cls, user, row_id):
        if row_id is None:
            return
        benefit_plan_id = cls.SCOPED_MODEL.objects.filter(id=row_id).values_list(
            'benefit_plan_id', flat=True
        ).first()
        assert_benefit_plan_in_scope(user, benefit_plan_id)

    @classmethod
    def validate_create(cls, user, **data):
        assert_benefit_plan_in_scope(user, data.get('benefit_plan_id'))
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        cls._assert_row_in_scope(user, data.get('id'))
        assert_benefit_plan_in_scope(user, data.get('benefit_plan_id'))
        super().validate_update(user, **data)

    @classmethod
    def validate_delete(cls, user, **data):
        cls._assert_row_in_scope(user, data.get('id'))
        super().validate_delete(user, **data)


class BeneficiaryValidation(BenefitPlanScopeValidationMixin, BaseModelValidation):
    OBJECT_TYPE = Beneficiary
    SCOPED_MODEL = Beneficiary


class GroupBeneficiaryValidation(BenefitPlanScopeValidationMixin, BaseModelValidation):
    OBJECT_TYPE = Beneficiary
    SCOPED_MODEL = GroupBeneficiary


def validate_project_unique_name(name, benefit_plan_id, uuid=None):
    instance = Project.objects.filter(
        name=name, benefit_plan__id=benefit_plan_id, is_deleted=False
    ).exclude(id=uuid).first()
    if instance:
        msg = "social_protection.validation.project.name_exists"
        return [{"message": _(msg % {'name': name})}]  # noqa: F504
    return []


class ProjectValidation(BaseModelValidation, ObjectExistsValidationMixin):
    OBJECT_TYPE = Project

    @classmethod
    def validate_create(cls, user, **data):
        assert_benefit_plan_in_scope(user, _project_benefit_plan(data))
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        current = Project.objects.filter(id=data.get('id')).values_list('benefit_plan_id', flat=True).first()
        assert_benefit_plan_in_scope(user, current)
        assert_benefit_plan_in_scope(user, _project_benefit_plan(data))
        super().validate_update(user, **data)

    @classmethod
    def validate_delete(cls, user, **data):
        current = Project.objects.filter(id=data.get('id')).values_list('benefit_plan_id', flat=True).first()
        assert_benefit_plan_in_scope(user, current)
        super().validate_delete(user, **data)

    @classmethod
    def validate_undo_delete(cls, data):
        obj_id = data.get('id')
        cls.validate_object_exists(obj_id)
        obj = Project.objects.get(id=obj_id)
        errors = validate_project_unique_name(
            obj.name, obj.benefit_plan_id, obj_id
        )
        if errors:
            raise ValidationError(errors)


def _project_benefit_plan(data):
    benefit_plan = data.get('benefit_plan')
    if benefit_plan is not None:
        return benefit_plan
    return data.get('benefit_plan_id')


def validate_programme_unique_code(code, uuid=None):
    instance = Programme.objects.filter(
        code=code, is_deleted=False
    ).exclude(id=uuid).first()
    if instance:
        msg = "social_protection.validation.programme.code_exists"
        return [{"message": _(msg % {'code': code})}]  # noqa: F504
    return []


class ProgrammeValidation(BaseModelValidation, ObjectExistsValidationMixin):
    OBJECT_TYPE = Programme

    @classmethod
    def validate_create(cls, user, **data):
        errors = [
            *validate_not_empty_field(data.get("code"), "code"),
            *validate_programme_unique_code(data.get('code')),
            *validate_not_empty_field(data.get("name"), "name"),
        ]
        if errors:
            raise ValidationError(errors)
        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        programme_id = data.get('id')
        cls.validate_object_exists(programme_id)
        assert_can_manage_programme(user, programme_id)
        errors = []
        if 'code' in data:
            errors += [
                *validate_not_empty_field(data.get("code"), "code"),
                *validate_programme_unique_code(data.get('code'), programme_id),
            ]
        if 'name' in data:
            errors += validate_not_empty_field(data.get("name"), "name")
        if errors:
            raise ValidationError(errors)
        super().validate_update(user, **data)

    @classmethod
    def validate_delete(cls, user, **data):
        programme_id = data.get('id')
        cls.validate_object_exists(programme_id)
        assert_can_manage_programme(user, programme_id)
        if BenefitPlan.objects.filter(programme_id=programme_id, is_deleted=False).exists():
            raise ValidationError(_("social_protection.validation.programme.has_benefit_plans"))
        super().validate_delete(user, **data)

    @classmethod
    def validate_members(cls, user, programme_id):
        cls.validate_object_exists(programme_id)
        if Programme.objects.filter(id=programme_id, is_deleted=True).exists():
            raise ValidationError(_("social_protection.programme.not_found"))
        assert_can_manage_programme(user, programme_id)

    @classmethod
    def validate_set_default(cls, user, programme_id):
        if not UserProgramme.objects.filter(
            user_id=user.id, programme_id=programme_id, is_deleted=False, programme__is_deleted=False,
        ).exists():
            raise ValidationError(_("social_protection.programme.not_a_member"))
