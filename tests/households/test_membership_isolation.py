import ast
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client

from apps.households.models import Household, Invitation, Membership, MembershipRole
from apps.households.permissions import current_household, require_household_member


@pytest.mark.django_db
def test_self_signup_creates_a_private_owner_household(client: Client, household):
    response = client.post(
        "/accounts/signup/",
        {
            "email": "New.Owner@example.test",
            "household_name": "  Weekend Crew  ",
            "password1": "Blueberry-Tent-Pencil-77!",
            "password2": "Blueberry-Tent-Pencil-77!",
        },
    )
    assert response.status_code == 302
    assert response["Location"] == "/accounts/login/"

    user = get_user_model().objects.get(email="new.owner@example.test")
    membership = user.memberships.get()
    assert membership.role == MembershipRole.OWNER
    assert membership.household.name == "Weekend Crew"
    assert membership.household != household
    assert user.memberships.count() == 1

    login_response = client.post(
        "/accounts/login/",
        {"username": "New.Owner@example.test", "password": "Blueberry-Tent-Pencil-77!"},
    )
    assert login_response.status_code == 302
    assert login_response["Location"] == "/"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("password1", "password2"),
    [
        ("Strong-owner-pass-123!", "Different-pass-123!"),
        ("short", "short"),
    ],
)
def test_self_signup_rejects_invalid_password_without_partial_records(
    client: Client, password1: str, password2: str
):
    before_households = Household.objects.count()
    response = client.post(
        "/accounts/signup/",
        {
            "email": "invalid@example.test",
            "household_name": "Temporary Home",
            "password1": password1,
            "password2": password2,
        },
    )
    assert response.status_code == 400
    assert not get_user_model().objects.filter(email="invalid@example.test").exists()
    assert Household.objects.count() == before_households
    assert Membership.objects.count() == 0


@pytest.mark.django_db
def test_self_signup_rejects_existing_email_without_partial_records(
    client: Client, owner
):
    before_households = Household.objects.count()
    before_memberships = Membership.objects.count()
    response = client.post(
        "/accounts/signup/",
        {
            "email": "OWNER@example.test",
            "household_name": "Attempted Duplicate",
            "password1": "Blueberry-Tent-Pencil-77!",
            "password2": "Blueberry-Tent-Pencil-77!",
        },
    )
    assert response.status_code == 400
    assert Household.objects.count() == before_households
    assert Membership.objects.count() == before_memberships


@pytest.mark.django_db
def test_self_signup_form_is_linked_from_login(client: Client):
    login = client.get("/accounts/login/")
    signup = client.get("/accounts/signup/")
    assert login.status_code == 200
    assert b"Create an account" in login.content
    assert signup.status_code == 200
    assert b"household_name" in signup.content


@pytest.mark.django_db
def test_valid_invitation_creates_member_and_consumes_invitation(client, invitation):
    invitation_page = client.get(f"/accounts/signup/{invitation.token}/")
    assert invitation_page.status_code == 200
    assert b"Join your CampKit household" in invitation_page.content
    assert b"starts a new private household" not in invitation_page.content
    response = client.post(
        f"/accounts/signup/{invitation.token}/",
        {
            "email": "new-member@example.test",
            "password1": "Safe-pass-123!",
            "password2": "Safe-pass-123!",
        },
    )
    assert response.status_code == 302
    user = get_user_model().objects.get(email="new-member@example.test")
    membership = user.memberships.get(household=invitation.household)
    assert membership.role == MembershipRole.MEMBER
    assert not Invitation.objects.filter(pk=invitation.pk).exists()


@pytest.mark.django_db
def test_existing_user_can_accept_invitation_and_switch_households(
    client, owner, other_owner, household, other_household
):
    invitation = Invitation.objects.create(
        household=other_household,
        email=owner.email,
        created_by=other_owner,
    )
    invitation_url = f"/accounts/signup/{invitation.token}/"

    invitation_page = client.get(invitation_url)
    assert invitation_page.status_code == 200
    assert b"Sign in to accept this invitation" in invitation_page.content
    assert b'name="password1"' not in invitation_page.content

    login = client.post(
        f"/accounts/login/?next={invitation_url}",
        {"username": owner.email.upper(), "password": "owner-pass-123"},
    )
    assert login.status_code == 302
    assert login["Location"] == invitation_url

    accept_page = client.get(invitation_url)
    assert b"Accept household invitation" in accept_page.content
    accepted = client.post(invitation_url, {})
    assert accepted.status_code == 302

    membership = Membership.objects.get(user=owner, household=other_household)
    assert membership.role == MembershipRole.MEMBER
    assert owner.memberships.count() == 2
    assert Household.objects.count() == 2
    assert not Invitation.objects.filter(pk=invitation.pk).exists()
    assert client.session["active_household_id"] == str(other_household.pk)

    switched = client.post(
        "/accounts/households/select/",
        {"household_id": str(household.pk), "next": "/"},
    )
    assert switched.status_code == 302
    assert client.session["active_household_id"] == str(household.pk)
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"Smith household" in dashboard.content


@pytest.mark.django_db
def test_user_cannot_switch_to_a_household_they_do_not_belong_to(
    client, owner, other_household
):
    client.force_login(owner)
    response = client.post(
        "/accounts/households/select/",
        {"household_id": str(other_household.pk), "next": "/"},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_login_without_active_household_prompts_for_selection(
    client, owner, other_household
):
    Membership.objects.create(
        user=owner,
        household=other_household,
        role=MembershipRole.MEMBER,
    )
    response = client.post(
        "/accounts/login/",
        {"username": owner.email.upper(), "password": "owner-pass-123"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/accounts/households/select/"
    selection = client.get(response["Location"])
    assert selection.status_code == 200
    assert b"Smith household" in selection.content
    assert b"Other household" in selection.content


@pytest.mark.django_db
def test_invitation_email_must_match(client: Client, invitation):
    response = client.post(
        f"/accounts/signup/{invitation.token}/",
        {
            "email": "attacker@example.test",
            "password1": "Safe-pass-123!",
            "password2": "Safe-pass-123!",
        },
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
    assert Invitation.objects.filter(
        household=household, email="next@example.test"
    ).exists()


@pytest.mark.django_db
def test_household_member_roles_and_cross_household_boundary(
    owner, member, household, other_owner, other_household
):
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
    assert b'name="username"' in response.content
    assert b'name="password"' in response.content


def test_domain_app_import_boundaries():
    root = Path(__file__).resolve().parents[2] / "apps"
    app_names = {
        "households",
        "inventory",
        "trips",
        "meals",
        "intake",
        "enrichment",
        "integrations",
    }
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
                if (
                    len(parts) >= 3
                    and parts[0] == "apps"
                    and parts[1] in app_names
                    and parts[1] != current_app
                    and parts[2] in {"models", "views"}
                ):
                    violations.append(f"{path}: imports {module}")
    assert violations == []
