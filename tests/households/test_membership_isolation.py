import ast
from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client, override_settings

from apps.households.models import Household, Invitation, MembershipRole
from apps.households.permissions import current_household, require_household_member


@pytest.mark.django_db
def test_signup_requires_an_invitation(client: Client):
    response = client.post(
        "/accounts/signup/",
        {"email": "public@example.test", "password1": "Safe-pass-123!", "password2": "Safe-pass-123!"},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_valid_invitation_creates_member_and_consumes_invitation(client, invitation):
    response = client.post(
        f"/accounts/signup/{invitation.token}/",
        {
            "email": "new-member@example.test",
            "password1": "Safe-pass-123!",
            "password2": "Safe-pass-123!",
        },
    )
    assert response.status_code == 302
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.get(email="new-member@example.test")
    membership = user.memberships.get(household=invitation.household)
    assert membership.role == MembershipRole.MEMBER
    assert not Invitation.objects.filter(pk=invitation.pk).exists()


@pytest.mark.django_db
def test_invitation_email_must_match(client: Client, invitation):
    response = client.post(
        f"/accounts/signup/{invitation.token}/",
        {"email": "attacker@example.test", "password1": "Safe-pass-123!", "password2": "Safe-pass-123!"},
    )
    assert response.status_code == 400
    assert Invitation.objects.filter(pk=invitation.pk).exists()


@pytest.mark.django_db
def test_only_owner_can_issue_an_invitation(client: Client, owner, member, household):
    client.force_login(member)
    denied = client.post("/accounts/invitations/", {"email": "next@example.test"})
    assert denied.status_code == 403

    client.force_login(owner)
    allowed = client.post("/accounts/invitations/", {"email": "next@example.test"})
    assert allowed.status_code == 201
    assert Invitation.objects.filter(household=household, email="next@example.test").exists()


@pytest.mark.django_db
def test_household_member_roles_and_cross_household_boundary(owner, member, household, other_owner, other_household):
    assert require_household_member(owner, household.pk) == household
    assert require_household_member(member, household.pk) == household
    with pytest.raises(PermissionDenied):
        require_household_member(member, other_household.pk)
    with pytest.raises(PermissionDenied):
        require_household_member(other_owner, household.pk)


@pytest.mark.django_db
def test_current_household_uses_authenticated_membership_and_rejects_ambiguous_context(
    owner, household, rf
):
    request = rf.get("/")
    request.user = owner
    assert current_household(request) == household


@pytest.mark.django_db
def test_login_page_uses_django_auth_form(client):
    response = client.get("/accounts/login/")
    assert response.status_code == 200
    assert b"name=\"username\"" in response.content
    assert b"name=\"password\"" in response.content


def test_domain_app_import_boundaries():
    root = Path(__file__).resolve().parents[2] / "apps"
    app_names = {"households", "inventory", "trips", "meals", "intake", "enrichment", "integrations"}
    violations = []
    for path in root.rglob("*.py"):
        if "migrations" in path.parts or "tests" in path.parts:
            continue
        current_app = path.relative_to(root).parts[0]
        if current_app not in app_names:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for module in imported:
                parts = module.split(".")
                if len(parts) >= 3 and parts[0] == "apps" and parts[1] in app_names and parts[1] != current_app:
                    if parts[2] in {"models", "views"}:
                        violations.append(f"{path}: imports {module}")
    assert violations == []
