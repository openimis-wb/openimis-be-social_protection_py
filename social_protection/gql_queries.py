import graphene
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from graphene import ObjectType
from graphene_django import DjangoObjectType
import graphene_django_optimizer as gql_optimizer
import django_filters

from contribution_plan.models import PaymentPlan
from core import prefix_filterset, ExtendedConnection
from individual.models import GroupIndividual
from individual.gql_queries import IndividualGQLType, GroupGQLType, \
    IndividualDataSourceUploadGQLType
from location.models import Location
from social_protection.apps import SocialProtectionConfig
from social_protection.programme_scope import (
    scope_by_current_rows,
    scope_memberships,
    scope_programmes,
    scope_queryset,
)
from social_protection.models import (
    Beneficiary,
    BenefitPlan,
    GroupBeneficiary,
    BenefitPlanDataUploadRecords,
    Activity,
    Project,
    BeneficiaryProjectTimeEntry,
    GroupBeneficiaryProjectTimeEntry,
    BeneficiaryProjectEnrollment,
    GroupBeneficiaryProjectEnrollment,
    Programme,
    UserProgramme,
)


def _have_permissions(user, permission):
    if isinstance(user, AnonymousUser):
        return False
    if not user.id:
        return False
    return user.has_perms(permission)


class BenefitPlanScopedTypeMixin:
    """
    Restricts every queryset graphene-django resolves for the type (root and
    nested connections, list fields, `node`) to rows whose benefit plans,
    reached through each of `benefit_plan_paths`, are in the programme scope.
    """
    benefit_plan_paths = ("benefit_plan",)

    @classmethod
    def get_queryset(cls, queryset, info):
        for path in cls.benefit_plan_paths:
            queryset = scope_queryset(queryset, info.context.user, info.context, path)
        return queryset


class JsonExtMixin:
    def resolve_json_ext(self, info):
        perms = SocialProtectionConfig.gql_schema_search_perms
        if _have_permissions(info.context.user, perms):
            return self.json_ext
        return None


class ProgrammeGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    @classmethod
    def get_queryset(cls, queryset, info):
        return scope_programmes(queryset, info.context.user, info.context)

    class Meta:
        model = Programme
        interfaces = (graphene.relay.Node,)
        fields = (
            "id", "code", "name", "description", "is_deleted",
            "date_created", "date_updated", "date_valid_from",
            "date_valid_to", "version",
        )
        filter_fields = {
            "id": ["exact"],
            "code": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "name": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "is_deleted": ["exact"],
        }
        connection_class = ExtendedConnection


class UserProgrammeGQLType(DjangoObjectType):
    """Membership of a user in a programme; the user is exposed through scalar fields only."""
    uuid = graphene.String(source='uuid')
    user_id = graphene.String()
    username = graphene.String()
    last_name = graphene.String()
    other_names = graphene.String()

    class Meta:
        model = UserProgramme
        interfaces = (graphene.relay.Node,)
        fields = ("id", "programme", "is_default", "date_created")
        filter_fields = {
            "id": ["exact"],
            "is_default": ["exact"],
            "user__username": ["exact", "iexact", "istartswith", "icontains"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        user = info.context.user
        return scope_memberships(
            queryset, user, info.context,
            include_co_members=_have_permissions(user, SocialProtectionConfig.gql_programme_update_perms),
        )

    def resolve_user_id(self, info):
        return str(self.user_id)

    def resolve_username(self, info):
        return self.user.username

    def resolve_last_name(self, info):
        return getattr(self.user.i_user, "last_name", None)

    def resolve_other_names(self, info):
        return getattr(self.user.i_user, "other_names", None)


class BenefitPlanGQLType(BenefitPlanScopedTypeMixin, DjangoObjectType, JsonExtMixin):
    benefit_plan_paths = ("",)
    uuid = graphene.String(source='uuid')
    has_payment_plans = graphene.Boolean()

    class Meta:
        model = BenefitPlan
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "code": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "name": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "max_beneficiaries": ["exact", "lt", "lte", "gt", "gte"],
            "institution": ["exact", "iexact", "startswith", "istartswith",
                            "contains", "icontains"],
            "type": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "description": ["exact", "iexact", "startswith", "istartswith",
                            "contains", "icontains"],
            "programme__id": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_beneficiary_data_schema(self, info):
        perms = SocialProtectionConfig.gql_schema_search_perms
        if _have_permissions(info.context.user, perms):
            return self.beneficiary_data_schema
        return None

    def resolve_has_payment_plans(self, info):
        return PaymentPlan.objects.filter(benefit_plan_id=self.id).exists()


class BeneficiaryProjectTimeEntryGQLType(BenefitPlanScopedTypeMixin, DjangoObjectType):
    benefit_plan_paths = ("enrollment__project__benefit_plan", "enrollment__beneficiary__benefit_plan")

    class Meta:
        model = BeneficiaryProjectTimeEntry
        fields = ("id", "day_number", "percent_complete")
        interfaces = (graphene.relay.Node,)


class GroupBeneficiaryProjectTimeEntryGQLType(BenefitPlanScopedTypeMixin, DjangoObjectType):
    benefit_plan_paths = ("enrollment__project__benefit_plan", "enrollment__group_beneficiary__benefit_plan")

    class Meta:
        model = GroupBeneficiaryProjectTimeEntry
        fields = ("id", "day_number", "percent_complete")
        interfaces = (graphene.relay.Node,)


class BeneficiarySharedFilterMixin:
    location_prefix = None  # must be defined in subclass

    def filter_is_eligible(self, queryset, name, value):
        return queryset.filter(is_eligible=value)

    def filter_location(self, queryset, name, value):
        if not value or not self.location_prefix:
            return queryset

        # Split multiple level filters (format: "level1:term1,level2:term2")
        level_filters = [f.strip() for f in value.split(',') if f.strip()]
        if not level_filters:
            return queryset

        location_q = Q()

        for level_filter in level_filters:
            if ':' not in level_filter:
                continue

            level_str, search_term = level_filter.split(':', 1)
            level = int(level_str.strip())
            search_term = search_term.strip()

            if not search_term or level < 0 or level > 3:
                continue

            # Determine the lookup path based on level (R=0, D=1, W=2, V=3):
            # For level 3 (Village), we look at group's location directly
            # For lower levels, we traverse up the parent chain
            parent_chain = '__'.join(['parent'] * (3 - level))
            if parent_chain:
                lookup = (
                    f"{self.location_prefix}location__"
                    f"{parent_chain}__name__icontains"
                )
            else:
                lookup = f"{self.location_prefix}location__name__icontains"
            location_q &= Q(**{lookup: search_term})

        return queryset.filter(location_q) if location_q else queryset

    def filter_eligible_for_project(self, queryset, name, value):
        """Filter beneficiaries eligible for enrollment in a project.

        If project.allows_multiple_enrollments is True:
            - Include beneficiaries not enrolled in any exclusive project
            - Include beneficiaries already enrolled in this project
        If project.allows_multiple_enrollments is False:
            - Include beneficiaries not enrolled in any project
            - Include beneficiaries already enrolled in this project
        """
        if not value:
            return queryset

        request = getattr(self, 'request', None)
        project = scope_queryset(
            Project.objects.filter(id=value), getattr(request, 'user', None), request
        ).get()
        enrollment_model = self._get_enrollment_model()
        beneficiary_field = self._get_beneficiary_field()

        if project.allows_multiple_enrollments:
            enrolled_in_exclusive = set(
                enrollment_model.objects.filter(
                    project__allows_multiple_enrollments=False,
                    is_deleted=False
                ).exclude(
                    project_id=project.id
                ).values_list(f'{beneficiary_field}_id', flat=True)
            )
            return queryset.exclude(id__in=enrolled_in_exclusive)
        else:
            enrolled_elsewhere = set(
                enrollment_model.objects.filter(
                    is_deleted=False
                ).exclude(
                    project_id=project.id
                ).values_list(f'{beneficiary_field}_id', flat=True)
            )
            return queryset.exclude(id__in=enrolled_elsewhere)

    def filter_enrolled_in_project(self, queryset, name, value):
        if not value:
            return queryset
        enrollment_model = self._get_enrollment_model()
        beneficiary_field = self._get_beneficiary_field()
        enrolled_ids = enrollment_model.objects.filter(
            project_id=value,
            is_deleted=False
        ).values_list(f'{beneficiary_field}_id', flat=True)
        return queryset.filter(id__in=enrolled_ids)

    def _get_enrollment_model(self):
        raise NotImplementedError(
            "Subclass must implement _get_enrollment_model")

    def _get_beneficiary_field(self):
        raise NotImplementedError(
            "Subclass must implement _get_beneficiary_field")


class BeneficiaryFilter(
    django_filters.FilterSet, BeneficiarySharedFilterMixin
):
    location_prefix = "individual__"
    is_eligible = django_filters.BooleanFilter(method='filter_is_eligible')
    search = django_filters.CharFilter(method='filter_search')
    location = django_filters.CharFilter(method='filter_location')
    eligible_for_project = django_filters.CharFilter(
        method='filter_eligible_for_project')
    enrolled_in_project = django_filters.CharFilter(
        method='filter_enrolled_in_project')

    class Meta:
        model = Beneficiary
        fields = {
            "id": ["exact"],
            "status": ["exact", "iexact", "startswith", "istartswith",
                       "contains", "icontains"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            **prefix_filterset("individual__",
                               IndividualGQLType._meta.filter_fields),
            **prefix_filterset("benefit_plan__",
                               BenefitPlanGQLType._meta.filter_fields),
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }

    def _get_enrollment_model(self):
        return BeneficiaryProjectEnrollment

    def _get_beneficiary_field(self):
        return 'beneficiary'

    def filter_search(self, queryset, name, value):
        if not value:
            return queryset

        village_matches = Location.objects.filter(
            type='V',
            validity_to__isnull=True,
        ).filter(
            Q(name__icontains=value)
            | Q(parent__name__icontains=value)
            | Q(parent__parent__name__icontains=value)
            | Q(parent__parent__parent__name__icontains=value)
        ).values_list('id', flat=True)

        return queryset.filter(
            Q(individual__first_name__icontains=value)
            | Q(individual__last_name__icontains=value)
            | Q(json_ext__icontains=value)
            | Q(individual__location__id__in=village_matches)
        )


class BeneficiaryProjectEnrollmentGQLType(BenefitPlanScopedTypeMixin, DjangoObjectType):
    benefit_plan_paths = ("project__benefit_plan", "beneficiary__benefit_plan")
    uuid = graphene.String(source='uuid')
    time_entries = graphene.List(BeneficiaryProjectTimeEntryGQLType)

    class Meta:
        model = BeneficiaryProjectEnrollment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "project__id": ["exact"],
            "is_deleted": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_time_entries(self, info, **kwargs):
        qs = BeneficiaryProjectTimeEntryGQLType.get_queryset(
            BeneficiaryProjectTimeEntry.objects.filter(enrollment=self, is_deleted=False), info)
        return gql_optimizer.query(qs, info)


class BeneficiaryGQLType(BenefitPlanScopedTypeMixin, DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    is_eligible = graphene.Boolean()
    project_enrollments = graphene.List(BeneficiaryProjectEnrollmentGQLType)

    class Meta:
        model = Beneficiary
        interfaces = (graphene.relay.Node,)
        filterset_class = BeneficiaryFilter
        connection_class = ExtendedConnection

    def resolve_is_eligible(self, info):
        return self.is_eligible

    def resolve_project_enrollments(self, info, **kwargs):
        qs = BeneficiaryProjectEnrollmentGQLType.get_queryset(
            BeneficiaryProjectEnrollment.objects.filter(beneficiary=self, is_deleted=False), info)
        return gql_optimizer.query(qs, info)


class GroupBeneficiaryFilter(
    django_filters.FilterSet, BeneficiarySharedFilterMixin
):
    location_prefix = "group__"
    is_eligible = django_filters.BooleanFilter(method='filter_is_eligible')
    search = django_filters.CharFilter(method='filter_search')
    location = django_filters.CharFilter(method='filter_location')
    eligible_for_project = django_filters.CharFilter(
        method='filter_eligible_for_project')
    enrolled_in_project = django_filters.CharFilter(
        method='filter_enrolled_in_project')

    class Meta:
        model = GroupBeneficiary
        fields = {
            "id": ["exact"],
            "status": ["exact", "iexact", "startswith", "istartswith",
                       "contains", "icontains"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            **prefix_filterset("group__",
                               GroupGQLType._meta.filter_fields),
            **prefix_filterset("benefit_plan__",
                               BenefitPlanGQLType._meta.filter_fields),
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }

    def _get_enrollment_model(self):
        return GroupBeneficiaryProjectEnrollment

    def _get_beneficiary_field(self):
        return 'group_beneficiary'

    def filter_search(self, queryset, name, value):
        if not value:
            return queryset

        head_matches = GroupIndividual.objects.filter(
            Q(individual__first_name__icontains=value)
            | Q(individual__last_name__icontains=value),
            role=GroupIndividual.Role.HEAD,
            is_deleted=False
        ).values_list('group_id', flat=True)

        village_matches = Location.objects.filter(
            type='V',
            validity_to__isnull=True,
        ).filter(
            Q(name__icontains=value)
            | Q(parent__name__icontains=value)
            | Q(parent__parent__name__icontains=value)
            | Q(parent__parent__parent__name__icontains=value)
        ).values_list('id', flat=True)

        return queryset.filter(
            Q(group__code__icontains=value)
            | Q(json_ext__icontains=value)
            | Q(group__id__in=head_matches)
            | Q(group__location__id__in=village_matches)
        )


class GroupBeneficiaryProjectEnrollmentGQLType(BenefitPlanScopedTypeMixin, DjangoObjectType):
    benefit_plan_paths = ("project__benefit_plan", "group_beneficiary__benefit_plan")
    uuid = graphene.String(source='uuid')
    time_entries = graphene.List(GroupBeneficiaryProjectTimeEntryGQLType)

    class Meta:
        model = GroupBeneficiaryProjectEnrollment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "project__id": ["exact"],
            "is_deleted": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_time_entries(self, info, **kwargs):
        qs = GroupBeneficiaryProjectTimeEntryGQLType.get_queryset(
            GroupBeneficiaryProjectTimeEntry.objects.filter(enrollment=self, is_deleted=False), info)
        return gql_optimizer.query(qs, info)


class GroupBeneficiaryGQLType(BenefitPlanScopedTypeMixin, DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    is_eligible = graphene.Boolean()
    project_enrollments = graphene.List(
        GroupBeneficiaryProjectEnrollmentGQLType)

    class Meta:
        model = GroupBeneficiary
        interfaces = (graphene.relay.Node,)
        filterset_class = GroupBeneficiaryFilter
        connection_class = ExtendedConnection

    def resolve_is_eligible(self, info):
        return self.is_eligible

    def resolve_project_enrollments(self, info, **kwargs):
        qs = GroupBeneficiaryProjectEnrollmentGQLType.get_queryset(
            GroupBeneficiaryProjectEnrollment.objects.filter(group_beneficiary=self, is_deleted=False), info)
        return gql_optimizer.query(qs, info)


class BenefitPlanDataUploadQGLType(BenefitPlanScopedTypeMixin, DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = BenefitPlanDataUploadRecords
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "workflow": ["exact", "iexact", "startswith", "istartswith",
                         "contains", "icontains"],
            **prefix_filterset(
                "data_upload__",
                IndividualDataSourceUploadGQLType._meta.filter_fields),
            **prefix_filterset("benefit_plan__",
                               BenefitPlanGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class BenefitPlanSchemaFieldsGQLType(ObjectType):
    schema_fields = graphene.List(graphene.String)

    def resolve_schema_fields(self, info, **kwargs):
        schemas = self.values_list(
            "beneficiary_data_schema__properties", flat=True)
        field_list = set(
            f'json_ext__{field}'
            for schema in schemas  # Iterate over each schema
            if schema  # Ensure the schema is not None or empty
            for field in schema  # Iterate over fields in the schema
        )
        return field_list


class BenefitPlanHistoryGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    has_payment_plans = graphene.Boolean()

    programme = graphene.Field(ProgrammeGQLType)

    @classmethod
    def get_queryset(cls, queryset, info):
        return scope_by_current_rows(queryset, BenefitPlan, info.context.user, info.context, benefit_plan_path="")

    def resolve_programme(self, info):
        """The programme recorded on this history row, when the user may see it."""
        if self.programme_id is None:
            return None
        return ProgrammeGQLType.get_queryset(Programme.objects.filter(id=self.programme_id), info).first()

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = BenefitPlan.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "code": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "name": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "max_beneficiaries": ["exact", "lt", "lte", "gt", "gte"],
            "institution": ["exact", "iexact", "startswith", "istartswith",
                            "contains", "icontains"],

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "description": ["exact", "iexact", "startswith", "istartswith",
                            "contains", "icontains"],
        }
        connection_class = ExtendedConnection

    def resolve_beneficiary_data_schema(self, info):
        perms = SocialProtectionConfig.gql_schema_search_perms
        if _have_permissions(info.context.user, perms):
            return self.beneficiary_data_schema
        return None

    def resolve_has_payment_plans(self, info):
        return PaymentPlan.objects.filter(benefit_plan_id=self.id).exists()


class ActivityFilter(django_filters.FilterSet):
    class Meta:
        model = Activity
        fields = {
            "id": ["exact"],
            "name": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }


class ActivityGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = Activity
        interfaces = (graphene.relay.Node,)
        filterset_class = ActivityFilter
        connection_class = ExtendedConnection


class ProjectFilter(django_filters.FilterSet):
    class Meta:
        model = Project
        fields = {
            "id": ["exact"],
            "name": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            'status': ['exact', 'icontains'],
            'benefit_plan__id': ['exact'],
            'activity__id': ['exact'],
            'location__id': ['exact'],
            'target_beneficiaries': ['exact', 'gte', 'lte'],
            'working_days': ['exact', 'gte', 'lte'],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }


class ProjectGQLType(BenefitPlanScopedTypeMixin, DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = Project
        interfaces = (graphene.relay.Node,)
        filterset_class = ProjectFilter
        connection_class = ExtendedConnection


class ProjectHistoryGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    benefit_plan = graphene.Field(BenefitPlanGQLType)

    @classmethod
    def get_queryset(cls, queryset, info):
        return scope_by_current_rows(queryset, Project, info.context.user, info.context)

    def resolve_benefit_plan(self, info):
        """The benefit plan recorded on this history row, when it is in the user's scope."""
        return BenefitPlanGQLType.get_queryset(BenefitPlan.objects.filter(id=self.benefit_plan_id), info).first()

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = Project.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "name": ["exact", "iexact", "startswith", "istartswith",
                     "contains", "icontains"],
            'status': ['exact', 'icontains'],
            'benefit_plan__id': ['exact'],
            'activity__id': ['exact'],
            'location__id': ['exact'],
            'target_beneficiaries': ['exact', 'gte', 'lte'],
            'working_days': ['exact', 'gte', 'lte'],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection
