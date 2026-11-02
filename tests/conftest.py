import pytest
from django.contrib.auth import get_user_model

from apps.households.models import Household, Invitation, Membership, MembershipRole


@pytest.fixture
def household(db):
    return Household.objects.create(name="Smith household")


@pytest.fixture
def owner(db, household):
    user = get_user_model().objects.create_user(
        username="owner@example.test", email="owner@example.test", password="owner-pass-123"
    )
    Membership.objects.create(user=user, household=household, role=MembershipRole.OWNER)
    return user


@pytest.fixture
def member(db, household):
    user = get_user_model().objects.create_user(
        username="member@example.test", email="member@example.test", password="member-pass-123"
    )
    Membership.objects.create(user=user, household=household, role=MembershipRole.MEMBER)
    return user


@pytest.fixture
def other_household(db):
    return Household.objects.create(name="Other household")


@pytest.fixture
def other_owner(db, other_household):
    user = get_user_model().objects.create_user(
        username="other@example.test", email="other@example.test", password="other-pass-123"
    )
    Membership.objects.create(user=user, household=other_household, role=MembershipRole.OWNER)
    return user


@pytest.fixture
def invitation(owner, household):
    return Invitation.objects.create(
        household=household,
        email="new-member@example.test",
        created_by=owner,
    )
