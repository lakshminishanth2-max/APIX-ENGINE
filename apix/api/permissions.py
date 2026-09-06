"""Role-based access control.

Roles are Django groups - ``ADMIN``, ``NSO_ANALYST``, ``RBI_RESEARCHER`` - and
are also embedded in the JWT so the React console can render the right
navigation without a second round trip.  The group membership on the server is
authoritative; the claim is a convenience, never a permission source.

Access model
------------
============================  =====  ============  ===============
Capability                    ADMIN  NSO_ANALYST   RBI_RESEARCHER
============================  =====  ============  ===============
Read published index series   yes    yes           yes
Read unpublished / suppressed yes    yes           no
Read canonical fares          yes    yes           yes (aggregated)
Read raw payloads             yes    yes           no
Trigger a collection run      yes    yes           no
Resume a halted source        yes    no            no
============================  =====  ============  ===============

RBI researchers deliberately cannot see suppressed values: a suppressed index is
one the NSO has judged unfit to publish, and exposing it downstream would defeat
the suppression.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import AbstractUser
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apix.enums import UserRole

__all__ = [
    "CanReadRawPayloads",
    "CanTriggerCollection",
    "HasAnyRole",
    "IsAdminRole",
    "IsNSOAnalyst",
    "IsStatisticalReader",
    "roles_for",
]


def roles_for(user: AbstractUser | Any) -> set[str]:
    """Effective roles: superusers implicitly hold every role."""
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        return {UserRole.ADMIN, UserRole.NSO_ANALYST, UserRole.RBI_RESEARCHER}
    return set(user.groups.values_list("name", flat=True)) & set(UserRole.values)


class HasAnyRole(BasePermission):
    """Base class: subclasses declare ``required_roles``."""

    required_roles: frozenset[str] = frozenset()
    message = "Your account does not hold a role permitting this operation."

    def has_permission(self, request: Request, view: APIView) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        held = roles_for(request.user)
        required = frozenset(getattr(view, "required_roles", self.required_roles))
        return bool(held & required) if required else bool(held)


class IsStatisticalReader(HasAnyRole):
    """Any authenticated, role-holding consumer of published statistics."""

    required_roles = frozenset({UserRole.ADMIN, UserRole.NSO_ANALYST, UserRole.RBI_RESEARCHER})


class IsNSOAnalyst(HasAnyRole):
    required_roles = frozenset({UserRole.ADMIN, UserRole.NSO_ANALYST})
    message = "Only NSO analysts and administrators may perform this operation."


class IsAdminRole(HasAnyRole):
    required_roles = frozenset({UserRole.ADMIN})
    message = "Administrator role required."


class CanTriggerCollection(HasAnyRole):
    """Dispatching a collection run consumes an external source's quota."""

    required_roles = frozenset({UserRole.ADMIN, UserRole.NSO_ANALYST})
    message = "Only NSO analysts and administrators may trigger a collection run."


class CanReadRawPayloads(HasAnyRole):
    """Raw payloads may carry commercially sensitive source formatting."""

    required_roles = frozenset({UserRole.ADMIN, UserRole.NSO_ANALYST})
    message = "Raw payload access is restricted to NSO analysts and administrators."


class ReadOnlyUnlessNSO(BasePermission):
    """Everyone with a role may read; only the NSO may write."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        held = roles_for(request.user)
        if not held:
            return False
        if request.method in SAFE_METHODS:
            return True
        return bool(held & {UserRole.ADMIN, UserRole.NSO_ANALYST})
