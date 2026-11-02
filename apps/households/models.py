import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def invitation_expiry():
    return timezone.now() + timezone.timedelta(days=7)


class Household(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class MembershipRole(models.TextChoices):
    OWNER = "owner", "Owner"
    MEMBER = "member", "Member"


class Membership(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=MembershipRole.choices, default=MembershipRole.MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("household", "user"), name="unique_household_membership"),
        ]
        indexes = [models.Index(fields=("user", "household"), name="membership_user_household_idx")]

    def __str__(self):
        return f"{self.user} — {self.household} ({self.role})"


class Invitation(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sent_household_invitations")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=invitation_expiry)

    class Meta:
        indexes = [models.Index(fields=("household", "email"), name="invite_household_email_idx")]

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()

    def __str__(self):
        return f"Invitation for {self.email}"
