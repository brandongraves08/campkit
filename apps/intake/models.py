import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class IntakeStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    NEEDS_REVIEW = "needs_review", "Needs review"
    ADDED = "added", "Added"
    REJECTED = "rejected", "Rejected"


class IntakeKind(models.TextChoices):
    GEAR = "gear", "Gear"
    PANTRY = "pantry", "Pantry"


class IntakeChannel(models.TextChoices):
    PHOTO = "photo", "Photo"
    BARCODE = "barcode", "Barcode"


class HouseholdIntakeManager(models.Manager):
    def for_household(self, household):
        return self.get_queryset().filter(household=household)


def _validate_confidence(value, field_name):
    if value is not None and not Decimal("0") <= value <= Decimal("1"):
        raise ValidationError({field_name: "Confidence must be between 0 and 1."})


class IntakeSubmission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    household = models.ForeignKey("households.Household", on_delete=models.CASCADE, related_name="intake_submissions")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     related_name="intake_submissions")
    kind = models.CharField(max_length=16, choices=IntakeKind.choices, default=IntakeKind.PANTRY)
    channel = models.CharField(max_length=16, choices=IntakeChannel.choices, default=IntakeChannel.PHOTO)
    status = models.CharField(max_length=16, choices=IntakeStatus.choices, default=IntakeStatus.PENDING)
    barcode = models.CharField(max_length=64, blank=True)
    dedupe_key = models.CharField(max_length=64)
    image_key = models.CharField(max_length=255, blank=True)
    image_content_type = models.CharField(max_length=100, blank=True)
    image_size_bytes = models.PositiveIntegerField(default=0)
    image_sha256 = models.CharField(max_length=64, blank=True)
    created_item_kind = models.CharField(max_length=16, blank=True)
    created_item_id = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    failure_reason = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = HouseholdIntakeManager()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # Rejected uploads may be resubmitted; every other duplicate is idempotent.
            models.UniqueConstraint(
                fields=("household", "dedupe_key"),
                condition=~models.Q(status=IntakeStatus.REJECTED),
                name="unique_open_household_intake_dedupe",
            ),
        ]
        indexes = [models.Index(fields=("household", "status"), name="intake_household_status_idx")]

    def clean(self):
        super().clean()
        if not self.dedupe_key:
            raise ValidationError({"dedupe_key": "Intake submissions require a dedupe key."})
        if self.channel == IntakeChannel.BARCODE and not self.barcode:
            raise ValidationError({"barcode": "Barcode intake requires a barcode value."})
        if self.channel == IntakeChannel.PHOTO and not self.image_sha256:
            raise ValidationError({"image": "Photo intake requires a stored image."})
        if self.image_size_bytes < 0:
            raise ValidationError({"image": "Uploaded image size cannot be negative."})

    def __str__(self):
        return f"{self.get_kind_display()} intake ({self.get_status_display()})"


class IntakeCandidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(IntakeSubmission, on_delete=models.CASCADE, related_name="candidate")
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=80, blank=True)
    brand = models.CharField(max_length=120, blank=True)
    model_number = models.CharField(max_length=120, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("1"))
    unit = models.CharField(max_length=20, blank=True)
    weight_grams = models.PositiveIntegerField(null=True, blank=True)
    nutrition = models.JSONField(default=dict, blank=True)
    allergens = models.JSONField(default=list, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=80)
    model_version = models.CharField(max_length=80, blank=True)
    overall_confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    identity_confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    quantity_confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    nutrition_confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    allergen_confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def clean(self):
        super().clean()
        if not self.name.strip():
            raise ValidationError({"name": "A candidate requires an item name."})
        if self.quantity < 0:
            raise ValidationError({"quantity": "Candidate quantity cannot be negative."})
        for field_name in ("overall_confidence", "identity_confidence", "quantity_confidence",
                           "nutrition_confidence", "allergen_confidence"):
            _validate_confidence(getattr(self, field_name), field_name)
        if not isinstance(self.nutrition, dict) or len(self.nutrition) > 32:
            raise ValidationError({"nutrition": "Nutrition data must be a small mapping."})
        if not isinstance(self.allergens, list) or len(self.allergens) > 32:
            raise ValidationError({"allergens": "Allergen data must be a short list."})
        if not isinstance(self.attributes, dict) or len(self.attributes) > 32:
            raise ValidationError({"attributes": "Attributes must be a small mapping."})

    def __str__(self):
        return self.name


class IntakeFieldEvidence(models.Model):
    candidate = models.ForeignKey(IntakeCandidate, on_delete=models.CASCADE, related_name="field_evidence")
    field_name = models.CharField(max_length=64)
    value = models.JSONField(null=True, blank=True)
    source = models.CharField(max_length=64)
    source_url = models.CharField(max_length=255, blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("field_name",)
        constraints = [
            models.UniqueConstraint(fields=("candidate", "field_name"), name="unique_candidate_field_evidence"),
        ]

    def clean(self):
        super().clean()
        if not self.field_name:
            raise ValidationError({"field_name": "Evidence requires a field name."})
        _validate_confidence(self.confidence, "confidence")
        if self.source_url and not self.source_url.lower().startswith(("https://", "http://")):
            raise ValidationError({"source_url": "Evidence links must use http or https."})

    def __str__(self):
        return f"{self.field_name} evidence"


class VerificationRequestStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class HermesDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Queued"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class VerificationRequest(models.Model):
    """A fact the app could not confirm, surfaced in-app and to Hermes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    household = models.ForeignKey("households.Household", on_delete=models.CASCADE,
                                  related_name="intake_verification_requests")
    submission = models.ForeignKey(IntakeSubmission, on_delete=models.CASCADE, null=True, blank=True,
                                   related_name="verification_requests")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="intake_verification_requests")
    reason = models.CharField(max_length=255)
    field_names = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=VerificationRequestStatus.choices,
                              default=VerificationRequestStatus.OPEN)
    channel = models.CharField(max_length=32, default="app_and_hermes")
    hermes_status = models.CharField(max_length=16, choices=HermesDeliveryStatus.choices,
                                     default=HermesDeliveryStatus.PENDING)
    hermes_attempts = models.PositiveSmallIntegerField(default=0)
    hermes_last_error = models.TextField(blank=True)
    hermes_payload = models.JSONField(default=dict, blank=True)
    hermes_queued_at = models.DateTimeField(null=True, blank=True)
    hermes_delivered_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=32, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="resolved_intake_verification_requests")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("submission",), condition=models.Q(status=VerificationRequestStatus.OPEN),
                                    name="unique_open_intake_verification"),
        ]
        indexes = [models.Index(fields=("household", "status"), name="intake_verif_household_idx")]

    def clean(self):
        super().clean()
        if self.status == VerificationRequestStatus.RESOLVED:
            if not self.resolved_at or not self.resolved_by_id:
                raise ValidationError("Resolved verification requests require a resolver and time.")
        elif self.resolved_at or self.resolved_by_id:
            raise ValidationError("Open verification requests cannot carry a resolution.")
        if not isinstance(self.field_names, list) or len(self.field_names) > 8:
            raise ValidationError({"field_names": "Verification requests track a short list of fields."})

    def __str__(self):
        return self.reason
