"""
Programme scope of benefit plans and of the rows they own.

The rule is applied by the models' `get_queryset(queryset, user)`, the core
row-security entry point that GraphQL types (`ScopedQuerysetMixin`), services
and mutations (`target_queryset`) and REST views all call. This module holds
what core does not provide: the membership scope of a user, the `X-Programme`
request header, and the write checks that answer with a programme message.

- A superuser sees every benefit plan.
- Any other authenticated user sees the benefit plans without programme and
  those of the programmes they belong to (live `UserProgramme` of a live
  `Programme`). An unauthenticated user sees none.
- While no benefit plan, deleted ones included, is attached to a programme,
  querysets are left unfiltered.
- The header `X-Programme` carries a programme UUID. When the user is a member
  of that programme (or a superuser and the programme is live), reads narrow
  to that programme's benefit plans; benefit plans without programme are
  excluded. Any other value raises `ProgrammeScopeError`; an empty header
  counts as absent. Writes ignore the header and check membership only.

The membership scope costs one query and is kept on the user instance it was
computed for, until `forget_programme_scope`. Core's object cache rebuilds
user instances from their field values, so another instance of the same
user computes its own scope.
"""
import dataclasses
import uuid
from typing import FrozenSet, Optional

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef, Q

PROGRAMME_HEADER = "X-Programme"
PROGRAMME_HEADER_META_KEY = "HTTP_" + PROGRAMME_HEADER.upper().replace("-", "_")

INVALID_HEADER_MESSAGE = "social_protection.programme.invalid_header"
OUT_OF_SCOPE_MESSAGE = "social_protection.programme.benefit_plan_out_of_scope"
NOT_A_MEMBER_MESSAGE = "social_protection.programme.not_a_member"
PROGRAMME_NOT_FOUND_MESSAGE = "social_protection.programme.not_found"
CHANGE_REQUIRES_SUPERUSER_MESSAGE = "social_protection.programme.change_requires_superuser"

_USER_SCOPE_ATTR = "_social_protection_programme_scope"
_REQUEST_HEADER_ATTR = "_social_protection_active_programme"


class ProgrammeScopeError(PermissionDenied):
    pass


@dataclasses.dataclass(frozen=True)
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

    @property
    def is_unrestricted(self):
        """True when every benefit plan is in scope."""
        return self.benefit_plan_q("") is None


def is_superuser(user):
    try:
        return bool(user.is_superuser)
    except (AttributeError, ValueError):
        return False


def _is_authenticated(user):
    return bool(user is not None and getattr(user, "is_authenticated", False) and getattr(user, "id", None))


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


def _build_membership_scope(user):
    if not _is_authenticated(user):
        return ProgrammeScope(is_authenticated=False, is_superuser=False, programme_ids=frozenset())
    if is_superuser(user):
        return ProgrammeScope(is_authenticated=True, is_superuser=True, programme_ids=frozenset())
    programme_ids, any_attached = _memberships_and_attachment(user)
    return ProgrammeScope(
        is_authenticated=True,
        is_superuser=False,
        programme_ids=programme_ids,
        any_benefit_plan_attached=any_attached,
    )


def _membership_scope(user):
    cached = getattr(user, _USER_SCOPE_ATTR, None) if user is not None else None
    if cached is not None:
        return cached
    scope = _build_membership_scope(user)
    if user is not None and scope.is_authenticated:
        try:
            setattr(user, _USER_SCOPE_ATTR, scope)
        except AttributeError:
            pass
    return scope


def forget_programme_scope(user):
    """Drops the membership scope kept on `user`, after a change of its memberships or attachments."""
    if user is None:
        return
    try:
        delattr(user, _USER_SCOPE_ATTR)
    except AttributeError:
        pass


def _header_value(request):
    if request is None:
        return None
    meta = getattr(request, "META", None) or {}
    value = meta.get(PROGRAMME_HEADER_META_KEY)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _active_programme_id(membership, raw_header):
    from social_protection.models import Programme
    if not membership.is_authenticated:
        raise ProgrammeScopeError(INVALID_HEADER_MESSAGE)
    try:
        programme_id = uuid.UUID(raw_header)
    except (TypeError, ValueError, AttributeError):
        raise ProgrammeScopeError(INVALID_HEADER_MESSAGE)
    if membership.is_superuser:
        if not Programme.objects.filter(id=programme_id, is_deleted=False).exists():
            raise ProgrammeScopeError(INVALID_HEADER_MESSAGE)
    elif programme_id not in membership.programme_ids:
        raise ProgrammeScopeError(INVALID_HEADER_MESSAGE)
    return programme_id


def get_programme_scope(user, request=None):
    """
    Scope of `user`. With a request, the `X-Programme` header applies; its
    validation is kept on the request for its later calls.
    """
    membership = _membership_scope(user)
    raw_header = _header_value(request)
    if raw_header is None:
        return membership
    user_key = getattr(user, "id", None)
    cached = getattr(request, _REQUEST_HEADER_ATTR, None)
    if cached is not None and cached[0] == user_key and cached[1] == raw_header:
        active_programme_id = cached[2]
    else:
        active_programme_id = _active_programme_id(membership, raw_header)
        try:
            setattr(request, _REQUEST_HEADER_ATTR, (user_key, raw_header, active_programme_id))
        except AttributeError:
            pass
    return dataclasses.replace(membership, active_programme_id=active_programme_id)


def filter_by_programme(queryset, user, benefit_plan_paths, request=None):
    """Rows of `queryset` whose benefit plans, reached through each of `benefit_plan_paths`, are in scope."""
    scope = get_programme_scope(user, request)
    for path in benefit_plan_paths:
        q = scope.benefit_plan_q(path)
        if q is not None:
            queryset = queryset.filter(q)
    return queryset


def scope_queryset(queryset, user, request=None, benefit_plan_path="benefit_plan"):
    """Restrict `queryset` to rows whose benefit plan, reached through `benefit_plan_path`, is in scope."""
    return filter_by_programme(queryset, user, (benefit_plan_path,), request)


def scope_benefit_plans(queryset, user, request=None):
    return filter_by_programme(queryset, user, ("",), request)


def scope_by_current_rows(queryset, model, user, request=None):
    """
    Rows of a history `queryset` whose current `model` row is in the programme
    scope of `user`, with the `X-Programme` header of `request`.
    """
    current = filter_by_programme(model.objects.all(), user, model.programme_scope_paths, request)
    if not current.query.has_filters():
        return queryset
    return queryset.filter(id__in=current.values("id"))


def narrow_to_active_programme(queryset, user, request, benefit_plan_paths):
    """
    With an `X-Programme` header on `request`, the rows of `queryset` whose
    benefit plans belong to that programme, as `user` may use it; `queryset`
    unchanged without the header.
    """
    if _header_value(request) is None:
        return queryset
    return filter_by_programme(queryset, user, benefit_plan_paths, request)


def scope_programmes(queryset, user):
    """Superusers see every programme; other users the live programmes they belong to."""
    scope = _membership_scope(user)
    if scope.is_superuser:
        return queryset
    if not scope.is_authenticated:
        return queryset.none()
    return queryset.filter(id__in=scope.programme_ids)


def scope_memberships(queryset, user, include_co_members=False):
    """
    Superusers see every membership; other users their own live memberships of
    live programmes and, with `include_co_members`, the live memberships of
    their programmes.
    """
    scope = _membership_scope(user)
    if scope.is_superuser:
        return queryset
    if not scope.is_authenticated:
        return queryset.none()
    q = Q(user_id=user.id, is_deleted=False, programme__is_deleted=False)
    if include_co_members:
        q |= Q(programme_id__in=scope.programme_ids, is_deleted=False)
    return queryset.filter(q)


def write_scope(user):
    """
    Scope a write by `user` is checked against, or None when the user may write
    on every benefit plan (superuser, or no benefit plan attached anywhere).
    """
    scope = _membership_scope(user)
    return None if scope.is_unrestricted else scope


def _visible_benefit_plan_ids(user, benefit_plan_ids, request=None):
    from social_protection.models import BenefitPlan
    queryset = BenefitPlan.get_queryset(BenefitPlan.objects.filter(id__in=benefit_plan_ids), user)
    queryset = narrow_to_active_programme(queryset, user, request, ("",))
    return set(queryset.values_list("id", flat=True))


def _as_benefit_plan_id(benefit_plan):
    benefit_plan_id = getattr(benefit_plan, "pk", benefit_plan)
    if isinstance(benefit_plan_id, uuid.UUID):
        return benefit_plan_id
    try:
        return uuid.UUID(str(benefit_plan_id))
    except (TypeError, ValueError, AttributeError):
        raise ProgrammeScopeError(OUT_OF_SCOPE_MESSAGE)


def assert_benefit_plan_in_scope(user, benefit_plan, request=None):
    """
    Raise `ProgrammeScopeError` unless `benefit_plan` (instance or id) is in the
    user's scope, as `BenefitPlan.get_queryset` and, with a request, the
    `X-Programme` header define it. None is skipped. When the scope is
    restricted, an id that matches no benefit plan is refused.
    """
    if benefit_plan is None:
        return
    scope = get_programme_scope(user, request)
    if scope.is_unrestricted:
        return
    benefit_plan_id = _as_benefit_plan_id(benefit_plan)
    if benefit_plan_id not in _visible_benefit_plan_ids(user, [benefit_plan_id], request):
        raise ProgrammeScopeError(OUT_OF_SCOPE_MESSAGE)


def assert_benefit_plans_in_scope(user, benefit_plan_ids):
    """Raise `ProgrammeScopeError` unless every benefit plan of `benefit_plan_ids` is in the user's write scope."""
    if write_scope(user) is None:
        return
    ids = {_as_benefit_plan_id(bp_id) for bp_id in benefit_plan_ids if bp_id is not None}
    if ids - _visible_benefit_plan_ids(user, ids):
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


def _assert_live_programme(programme_id):
    from social_protection.models import Programme
    if not Programme.objects.filter(id=programme_id, is_deleted=False).exists():
        raise ProgrammeScopeError(PROGRAMME_NOT_FOUND_MESSAGE)


def assert_can_create_in_programme(user, programme_id):
    """A new benefit plan goes into a live programme the creator belongs to; superusers use any live one."""
    programme_id = _as_uuid(programme_id)
    if programme_id is None:
        return
    _assert_live_programme(programme_id)
    if not is_superuser(user) and not is_programme_member(user, programme_id):
        raise ProgrammeScopeError(NOT_A_MEMBER_MESSAGE)


def assert_can_change_programme(user, target_programme_id):
    """
    Attaching, moving or detaching an existing benefit plan is reserved to
    superusers; a target programme must be live.
    """
    target_programme_id = _as_uuid(target_programme_id)
    if not is_superuser(user):
        raise ProgrammeScopeError(CHANGE_REQUIRES_SUPERUSER_MESSAGE)
    if target_programme_id is not None:
        _assert_live_programme(target_programme_id)


def benefit_plan_id_of(model, row_id):
    """The current benefit plan id of the `model` row `row_id`, or None when there is no such row."""
    try:
        return model.objects.filter(id=row_id).values_list("benefit_plan_id", flat=True).first()
    except (ValidationError, ValueError):
        return None
