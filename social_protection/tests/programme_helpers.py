from core.models import Role, RoleRight, User
from core.test_helpers import create_test_interactive_user
from individual.apps import IndividualConfig
from social_protection.apps import SocialProtectionConfig
from social_protection.models import BenefitPlan, Programme, UserProgramme
from social_protection.tests.test_helpers import create_benefit_plan


def social_protection_right_ids():
    """Every right id declared by social_protection and individual permission config."""
    right_ids = set()
    for config in (SocialProtectionConfig, IndividualConfig):
        for name in dir(config):
            if name.startswith("gql_") and name.endswith("_perms"):
                for right in getattr(config, name) or []:
                    right_ids.add(int(right))
    return right_ids


def get_admin_user():
    user = User.objects.filter(username="Admin", i_user__isnull=False).first()
    return user or create_test_interactive_user(username="Admin")


def create_user_with_rights(username, right_ids=None):
    """Interactive user who is not a superuser, holding `right_ids` (default: every module right)."""
    role = Role.objects.create(
        name=f"ProgrammeTestRole_{username}", is_system=0, is_blocked=False, audit_user_id=-1,
    )
    for right_id in (social_protection_right_ids() if right_ids is None else right_ids):
        RoleRight.objects.create(role=role, right_id=right_id, audit_user_id=-1)
    user = create_test_interactive_user(username=username, roles=[role.id])
    return User.objects.get(id=user.id)


def create_programme(code, username, is_deleted=False, name=None):
    programme = Programme(code=code, name=name or f"Programme {code}", is_deleted=is_deleted)
    programme.save(username=username)
    return programme


def add_member(user, programme, username, is_default=False, is_deleted=False):
    membership = UserProgramme(
        user=user, programme=programme, is_default=is_default, is_deleted=is_deleted,
    )
    membership.save(username=username)
    return membership


def create_programme_benefit_plan(code, programme, username, **override):
    benefit_plan = create_benefit_plan(username, payload_override={
        "code": code, "name": f"Plan {code}", **override,
    })
    if programme is not None:
        benefit_plan.programme = programme
        benefit_plan.save(username=username)
    return BenefitPlan.objects.get(id=benefit_plan.id)
