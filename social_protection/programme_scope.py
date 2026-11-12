"""
Programme scope of benefit plans and of the rows they own.

- A superuser sees every benefit plan.
- Any other authenticated user sees the benefit plans without programme and
  those of the programmes they belong to (live `UserProgramme` of a live
  `Programme`). An unauthenticated user sees none.
- The request header `X-Programme` carries a programme UUID. When the user is
  a member of that programme (or a superuser and the programme is live), the
  set narrows to that programme's benefit plans; benefit plans without
  programme are excluded. Any other value raises `ProgrammeScopeError`.
  An empty header counts as absent.

Read paths pass the request so the header applies; write paths call
`assert_benefit_plan_in_scope` without a request and check membership only.
The scope costs one query, computed once per request and kept on the request
object. While no benefit plan is attached to any programme, read querysets
are left unfiltered.
"""
import uuid
from dataclasses import dataclass
from typing import FrozenSet, Optional

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef, Q

PROGRAMME_HEADER = "X-Programme"
PROGRAMME_HEADER_META_KEY = "HTTP_X_PROGRAMME"

INVALID_HEADER_MESSAGE = "social_protection.programme.invalid_header"
OUT_OF_SCOPE_MESSAGE = "social_protection.programme.benefit_plan_out_of_scope"
NOT_A_MEMBER_MESSAGE = "social_protection.programme.not_a_member"
PROGRAMME_NOT_FOUND_MESSAGE = "social_protection.programme.not_found"

_REQUEST_CACHE_ATTR = "_social_protection_programme_scope"


class ProgrammeScopeError(PermissionDenied):
    pass


@dataclass(frozen=True)
class ProgrammeScope:
    is_authenticated: bool
    is_superuser: bool
    programme_ids: FrozenSet[uuid.UUID]
    active_programme_id: Optional[uuid.UUID] = None
    any_benefit_plan_attached: bool = True

    def benefit_plan_q(self, benefit_plan_path=""):
        """
        Q restricting a queryset whose benefit plan is reached through
        `benefit_plan_path` ("" for BenefitPlan itself); None means no restriction.
        """
        prefix = f"{benefit_plan_path}__" if benefit_plan_path else ""
        if not self.is_authenticated:
            return Q(pk__in=[])
        if self.active_programme_id is not None:
            return Q(**{f"{prefix}programme_id": self.active_programme_id})
        if self.is_superuser or not self.any_benefit_plan_attached:
            return None
        q = Q(**{f"{prefix}programme__isnull": True})
        if self.programme_ids:
            q |= Q(**{f"{prefix}programme_id__in": self.programme_ids})
        return q

    def allows_programme(self, programme_id):
        """True when a benefit plan attached to `programme_id` (None: no programme) is in scope."""
        if not self.is_authenticated:
            return False
        if self.active_programme_id is not None:
            return programme_id == self.active_programme_id
        if self.is_superuser:
            return True
        return programme_id is None or programme_id in self.programme_ids


def is_superuser(user):
    try:
        return bool(user.is_superuser)
    except (AttributeError, ValueError):
        return False


def _is_authenticated(user):
    return bool(user is not None and getattr(user, "is_authenticated", False) and getattr(user, "id", None))


def user_programme_ids(user):
    """Ids of the live programmes the user is a live member of."""
    from social_protection.models import UserProgramme
    if not _is_authenticated(user):
        return frozenset()
    return frozenset(
        UserProgramme.objects.filter(
            user_id=user.id, is_deleted=False, programme__is_deleted=False,
        ).values_list("programme_id", flat=True)
    )


def _memberships_and_attachment(user):
    """
    One query returning the ids of the user's live programmes and whether any
    benefit plan, deleted ones included, is attached to a programme.
    """
    from social_protection.models import BenefitPlan, Programme, UserProgramme
    rows = Programme.objects.annotate(
        attached=Exists(BenefitPlan.objects.filter(programme_id=OuterRef("pk"))),
        member=Exists(UserProgramme.objects.filter(
            programme_id=OuterRef("pk"), user_id=user.id, is_deleted=False,
        )),
    ).filter(Q(attached=True) | Q(member=True, is_deleted=False)).values_list(
        "id", "is_deleted", "attached", "member",
    )
    programme_ids = set()
    any_attached = False
    for programme_id, deleted, attached, member in rows:
        any_attached = any_attached or attached
        if member and not deleted:
            programme_ids.add(programme_id)
    return frozenset(programme_ids), any_attached


def _header_value(request):
    if request is None:
        return None
    meta = getattr(request, "META", None) or {}
    value = meta.get(PROGRAMME_HEADER_META_KEY)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_programme_id(raw):
    try:
        return uuid.UUID(raw)
    except (TypeError, ValueError, AttributeError):
        raise ProgrammeScopeError(INVALID_HEADER_MESSAGE)


def _build_scope(user, raw_header):
    from social_protection.models import Programme
    if not _is_authenticated(user):
        if raw_header is not None:
            raise ProgrammeScopeError(INVALID_HEADER_MESSAGE)
        return ProgrammeScope(is_authenticated=False, is_superuser=False, programme_ids=frozenset())

    superuser = is_superuser(user)
    programme_ids, any_attached = (frozenset(), True) if superuser else _memberships_and_attachment(user)
    active_programme_id = None
    if raw_header is not None:
        active_programme_id = _parse_programme_id(raw_header)
        if superuser:
            if not Programme.objects.filter(id=active_programme_id, is_deleted=False).exists():
                raise ProgrammeScopeError(INVALID_HEADER_MESSAGE)
        elif active_programme_id not in programme_ids:
            raise ProgrammeScopeError(INVALID_HEADER_MESSAGE)
    return ProgrammeScope(
        is_authenticated=True,
        is_superuser=superuser,
        programme_ids=programme_ids,
        active_programme_id=active_programme_id,
        any_benefit_plan_attached=any_attached,
    )


def get_programme_scope(user, request=None):
    """
    Scope of `user`. With a request, the `X-Programme` header applies and the
    result is kept on the request for its later calls.
    """
    raw_header = _header_value(request)
    user_key = getattr(user, "id", None)
    if request is not None:
        cached = getattr(request, _REQUEST_CACHE_ATTR, None)
        if cached is not None and cached[0] == user_key and cached[1] == raw_header:
            return cached[2]
    scope = _build_scope(user, raw_header)
    if request is not None:
        try:
            setattr(request, _REQUEST_CACHE_ATTR, (user_key, raw_header, scope))
        except AttributeError:
            pass
    return scope


def scope_queryset(queryset, user, request=None, benefit_plan_path="benefit_plan"):
    """Restrict `queryset` to rows whose benefit plan, reached through `benefit_plan_path`, is in scope."""
    q = get_programme_scope(user, request).benefit_plan_q(benefit_plan_path)
    return queryset if q is None else queryset.filter(q)


def scope_benefit_plans(queryset, user, request=None):
    return scope_queryset(queryset, user, request, benefit_plan_path="")


def scope_by_current_rows(queryset, model, user, request=None, benefit_plan_path="benefit_plan"):
    """
    Restrict `queryset` (e.g. a history queryset) to the ids of the current
    `model` rows whose benefit plan is in scope.
    """
    q = get_programme_scope(user, request).benefit_plan_q(benefit_plan_path)
    if q is None:
        return queryset
    return queryset.filter(id__in=model.objects.filter(q).values("id"))


def _programme_id_of(benefit_plan):
    from social_protection.models import BenefitPlan
    if isinstance(benefit_plan, BenefitPlan):
        return True, benefit_plan.programme_id
    if benefit_plan is None:
        return False, None
    try:
        rows = list(BenefitPlan.objects.filter(id=benefit_plan).values_list("programme_id", flat=True)[:1])
    except (ValidationError, ValueError):
        return False, None
    if not rows:
        return False, None
    return True, rows[0]


def can_access_benefit_plan(user, benefit_plan, request=None):
    """
    True when `benefit_plan` (instance or id) exists and is in the user's scope.
    Without a request, membership alone decides.
    """
    scope = get_programme_scope(user, request)
    if scope.is_authenticated and scope.is_superuser and scope.active_programme_id is None:
        return True
    exists, programme_id = _programme_id_of(benefit_plan)
    return exists and scope.allows_programme(programme_id)


def assert_benefit_plan_in_scope(user, benefit_plan, request=None):
    """
    Raise `ProgrammeScopeError` when `benefit_plan` (instance or id) exists and
    is out of the user's scope. A missing benefit plan is left to the caller's
    own existence check.
    """
    scope = get_programme_scope(user, request)
    if scope.is_authenticated and scope.is_superuser and scope.active_programme_id is None:
        return
    exists, programme_id = _programme_id_of(benefit_plan)
    if exists and not scope.allows_programme(programme_id):
        raise ProgrammeScopeError(OUT_OF_SCOPE_MESSAGE)


def assert_benefit_plans_in_scope(user, benefit_plan_ids):
    """Raise `ProgrammeScopeError` when any of the existing benefit plans in `benefit_plan_ids` is out of scope."""
    from social_protection.models import BenefitPlan
    scope = get_programme_scope(user)
    if scope.is_authenticated and scope.is_superuser:
        return
    ids = {bp_id for bp_id in benefit_plan_ids if bp_id is not None}
    if not ids:
        return
    programme_ids = set(BenefitPlan.objects.filter(id__in=ids).values_list("programme_id", flat=True))
    if any(not scope.allows_programme(programme_id) for programme_id in programme_ids):
        raise ProgrammeScopeError(OUT_OF_SCOPE_MESSAGE)


def is_programme_member(user, programme_id):
    """True for a live membership of a live programme."""
    from social_protection.models import UserProgramme
    if not _is_authenticated(user) or programme_id is None:
        return False
    return UserProgramme.objects.filter(
        user_id=user.id, programme_id=programme_id, is_deleted=False, programme__is_deleted=False,
    ).exists()


def assert_can_manage_programme(user, programme_id):
    """Superusers manage every programme; other users only the programmes they belong to."""
    if is_superuser(user):
        return
    if not is_programme_member(user, programme_id):
        raise ProgrammeScopeError(NOT_A_MEMBER_MESSAGE)


def _as_uuid(value):
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ProgrammeScopeError(PROGRAMME_NOT_FOUND_MESSAGE)


def assert_can_set_programme(user, target_programme_id, current_programme_id=None):
    """
    Attaching a benefit plan to `target_programme_id` (None: no programme)
    requires a live target programme and, for users other than superusers,
    membership of the target and of the current programme when moving.
    """
    from social_protection.models import Programme
    target_programme_id = _as_uuid(target_programme_id)
    current_programme_id = _as_uuid(current_programme_id)
    if target_programme_id is not None and not Programme.objects.filter(
        id=target_programme_id, is_deleted=False
    ).exists():
        raise ProgrammeScopeError(PROGRAMME_NOT_FOUND_MESSAGE)
    if is_superuser(user) or target_programme_id == current_programme_id:
        return
    for programme_id in {target_programme_id, current_programme_id}:
        if programme_id is not None and not is_programme_member(user, programme_id):
            raise ProgrammeScopeError(NOT_A_MEMBER_MESSAGE)
