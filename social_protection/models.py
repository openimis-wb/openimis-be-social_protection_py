from django.db import models
from django.db.models import Func
from django.utils.translation import gettext as _
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator

from core import models as core_models
from core.models import UUIDModel, ObjectMutation, MutationLog
from individual.models import Individual, Group, IndividualDataSourceUpload
from location.models import Location


class BeneficiaryStatus(models.TextChoices):
    POTENTIAL = "POTENTIAL", _("POTENTIAL")
    ACTIVE = "ACTIVE", _("ACTIVE")
    GRADUATED = "GRADUATED", _("GRADUATED")
    SUSPENDED = "SUSPENDED", _("SUSPENDED")


class Programme(core_models.HistoryBusinessModel):
    """
    Umbrella grouping benefit plans. Users see the benefit plans of the
    programmes they belong to (see `social_protection.programme_scope`).
    """
    code = models.CharField(max_length=32, null=False)
    name = models.CharField(max_length=255, null=False)
    description = models.CharField(max_length=1024, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['code'],
                condition=models.Q(is_deleted=False),
                name='social_protection_programme_unique_live_code',
            ),
        ]

    def __str__(self):
        return f'Programme {self.code}'


class BenefitPlan(core_models.HistoryBusinessModel):
    class BenefitPlanType(models.TextChoices):
        INDIVIDUAL_TYPE = "INDIVIDUAL", _("INDIVIDUAL")
        GROUP_TYPE = "GROUP", _("GROUP")

    code = models.CharField(max_length=8, null=False)
    name = models.CharField(max_length=255, null=False)
    max_beneficiaries = models.SmallIntegerField(null=True, blank=True)
    ceiling_per_beneficiary = models.DecimalField(
        max_digits=18, decimal_places=2, blank=True, null=True,
    )
    institution = models.CharField(max_length=255, null=True, blank=True)
    beneficiary_data_schema = models.JSONField(null=True, blank=True)
    type = models.CharField(
        max_length=100, choices=BenefitPlanType.choices, default=BenefitPlanType.INDIVIDUAL_TYPE, null=False
    )
    description = models.CharField(max_length=1024, null=True, blank=True)
    programme = models.ForeignKey(
        Programme, models.DO_NOTHING, null=True, blank=True, related_name="benefit_plans"
    )

    def __str__(self):
        return f'Benefit Plan {self.code}'


class UserProgramme(core_models.HistoryModel):
    """
    Membership of a user in a programme. A row is live while `is_deleted` is
    False; a user has at most one live row per programme and at most one live
    default programme.
    """
    user = models.ForeignKey(core_models.User, models.DO_NOTHING, related_name="+")
    programme = models.ForeignKey(Programme, models.DO_NOTHING, related_name="user_programmes")
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'programme'],
                condition=models.Q(is_deleted=False),
                name='social_protection_userprogramme_unique_live_member',
            ),
            models.UniqueConstraint(
                fields=['user'],
                condition=models.Q(is_deleted=False, is_default=True),
                name='social_protection_userprogramme_unique_live_default',
            ),
        ]

    def __str__(self):
        return f'{self.user_id} in {self.programme_id}'


class BenefitPlanMutation(UUIDModel, ObjectMutation):
    benefit_plan = models.ForeignKey(BenefitPlan, models.DO_NOTHING, related_name='mutations')
    mutation = models.ForeignKey(MutationLog, models.DO_NOTHING, related_name='benefit_plan')


class Activity(core_models.HistoryBusinessModel):
    name = models.CharField(max_length=255, null=False, unique=True)

    class Meta:
        verbose_name = "Activity"
        verbose_name_plural = "Activities"


class ProjectStatus(models.TextChoices):
    PREPARATION = "PREPARATION", _("PREPARATION")
    IN_PROGRESS = "IN_PROGRESS", _("IN PROGRESS")
    COMPLETED = "COMPLETED", _("COMPLETED")


class Project(core_models.HistoryBusinessModel):
    benefit_plan = models.ForeignKey(BenefitPlan, models.DO_NOTHING, null=False)
    name = models.CharField(max_length=255, null=False)
    status = models.CharField(
        max_length=100,
        choices=ProjectStatus.choices,
        default=ProjectStatus.PREPARATION,
        null=False
    )
    activity = models.ForeignKey(Activity, models.DO_NOTHING, null=False)
    location = models.ForeignKey(Location, models.DO_NOTHING, null=False)
    target_beneficiaries = models.SmallIntegerField(null=False)
    working_days = models.SmallIntegerField(null=False)
    allows_multiple_enrollments = models.BooleanField(default=False)


class ProjectMutation(UUIDModel, ObjectMutation):
    project = models.ForeignKey(Project, models.DO_NOTHING, related_name='mutations')
    mutation = models.ForeignKey(MutationLog, models.DO_NOTHING, related_name='project')


class BeneficiaryProjectEnrollment(core_models.HistoryBusinessModel):
    beneficiary = models.ForeignKey(
        'Beneficiary',
        models.DO_NOTHING,
        related_name='project_enrollments',
        null=False,
    )
    project = models.ForeignKey(
        Project,
        models.DO_NOTHING,
        related_name='beneficiary_enrollments',
        null=False,
    )

    class Meta:
        unique_together = ('beneficiary', 'project')
        verbose_name = _("Beneficiary Project Enrollment")
        verbose_name_plural = _("Beneficiary Project Enrollments")

    def clean(self):
        if self.beneficiary.status != BeneficiaryStatus.ACTIVE:
            raise ValidationError(_("Only ACTIVE beneficiaries can be enrolled in a project."))
        if self.project.benefit_plan_id != self.beneficiary.benefit_plan_id:
            raise ValidationError(_("Beneficiary and project must belong to the same program."))
        super().clean()

    def __str__(self):
        return f"{self.beneficiary} - {self.project.name}"


class GroupBeneficiaryProjectEnrollment(core_models.HistoryBusinessModel):
    group_beneficiary = models.ForeignKey(
        'GroupBeneficiary',
        models.DO_NOTHING,
        related_name='project_enrollments',
        null=False,
    )
    project = models.ForeignKey(
        Project,
        models.DO_NOTHING,
        related_name='group_beneficiary_enrollments',
        null=False,
    )

    class Meta:
        unique_together = ('group_beneficiary', 'project')
        verbose_name = _("Group Beneficiary Project Enrollment")
        verbose_name_plural = _("Group Beneficiary Project Enrollments")

    def clean(self):
        if self.group_beneficiary.status != BeneficiaryStatus.ACTIVE:
            raise ValidationError(_("Only ACTIVE group beneficiaries can be enrolled in a project."))
        if self.project.benefit_plan_id != self.group_beneficiary.benefit_plan_id:
            raise ValidationError(_("Group beneficiary and project must belong to the same program."))
        super().clean()

    def __str__(self):
        return f"{self.group_beneficiary.group.code} - {self.project.name}"


class Beneficiary(core_models.HistoryBusinessModel):
    individual = models.ForeignKey(Individual, models.DO_NOTHING, null=False)
    benefit_plan = models.ForeignKey(BenefitPlan, models.DO_NOTHING, null=False)
    status = models.CharField(max_length=100, choices=BeneficiaryStatus.choices, null=False)

    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    def clean(self):
        if self.benefit_plan.type != BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE:
            raise ValidationError(_("Beneficiary must be associated with an individual benefit plan."))
        super().clean()

    def __str__(self):
        return f'{self.individual.first_name} {self.individual.last_name}'

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()

        individuals = Individual.objects.filter(
            id__in=queryset.values('individual_id')
        ).distinct()

        individual_queryset = Individual.get_queryset(individuals, user)
        return queryset.filter(individual__in=individual_queryset)


class AbstractProjectTimeEntry(core_models.HistoryBusinessModel):
    """
    Base model for recording daily percent completion for enrollments in projects.
    Subclasses must implement `_get_enrollment_instance()`.
    """
    # Sequential workday number within the project (1..working_days).
    day_number = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    # Percentage of work completed for this day (0–100).
    percent_complete = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    class Meta:
        abstract = True

    def _get_enrollment_instance(self):
        """
        Subclasses must override this to return the enrollment object.
        """
        raise NotImplementedError("_get_enrollment_instance() must be implemented in subclass")

    def clean(self):
        enrollment = self._get_enrollment_instance()
        project = enrollment.project

        if not 1 <= self.day_number <= project.working_days:
            raise ValidationError(
                _("Day number must be between 1 and %(working_days)s.") % {"working_days": project.working_days}
            )

        super().clean()

    def __str__(self):
        enrollment = self._get_enrollment_instance()
        return f"{enrollment} - Day {self.day_number}: {self.percent_complete}%"


class BeneficiaryProjectTimeEntry(AbstractProjectTimeEntry):
    enrollment = models.ForeignKey(
        'BeneficiaryProjectEnrollment',
        models.DO_NOTHING,
        related_name='time_entries',
        null=False,
    )

    class Meta:
        unique_together = ('enrollment', 'day_number')
        verbose_name = _("Beneficiary Project Time Entry")
        verbose_name_plural = _("Beneficiary Project Time Entries")

    def _get_enrollment_instance(self):
        return self.enrollment


class GroupBeneficiaryProjectTimeEntry(AbstractProjectTimeEntry):
    enrollment = models.ForeignKey(
        'GroupBeneficiaryProjectEnrollment',
        models.DO_NOTHING,
        related_name='time_entries',
        null=False,
    )

    class Meta:
        unique_together = ('enrollment', 'day_number')
        verbose_name = _("Group Beneficiary Project Time Entry")
        verbose_name_plural = _("Group Beneficiary Project Time Entries")

    def _get_enrollment_instance(self):
        return self.enrollment


class BenefitPlanDataUploadRecords(core_models.HistoryModel):
    data_upload = models.ForeignKey(IndividualDataSourceUpload, models.DO_NOTHING, null=False)
    benefit_plan = models.ForeignKey(BenefitPlan, models.DO_NOTHING, null=False)
    workflow = models.CharField(max_length=50)

    def __str__(self):
        return f"{self.benefit_plan.code} {self.data_upload.source_name} {self.workflow} {self.date_created}"


class GroupBeneficiary(core_models.HistoryBusinessModel):
    group = models.ForeignKey(Group, models.DO_NOTHING, null=False)
    benefit_plan = models.ForeignKey(BenefitPlan, models.DO_NOTHING, null=False)
    status = models.CharField(max_length=100, choices=BeneficiaryStatus.choices, null=False)

    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    def clean(self):
        if self.benefit_plan.type != BenefitPlan.BenefitPlanType.GROUP_TYPE:
            raise ValidationError(_("Group beneficiary must be associated with a benefit plan type = GROUP."))
        super().clean()

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()

        groups = Group.objects.filter(
            id__in=queryset.values('group_id')
        ).distinct()

        group_queryset = Group.get_queryset(groups, user)
        return queryset.filter(group__in=group_queryset)


class JSONUpdate(Func):
    function = 'JSONB_SET'
    arity = 3
