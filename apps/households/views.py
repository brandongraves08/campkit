from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseGone, HttpResponseNotFound
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.households.models import Invitation, Membership, MembershipRole
from apps.households.permissions import current_household, require_household_owner


class InvitationSignupForm(forms.Form):
    email = forms.EmailField()
    password1 = forms.CharField(widget=forms.PasswordInput)
    password2 = forms.CharField(widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") and cleaned.get("password2") and cleaned["password1"] != cleaned["password2"]:
            self.add_error("password2", "Passwords do not match.")
        return cleaned


def signup_without_invitation(request):
    return HttpResponseForbidden("Sign-up is invitation-only.")


def accept_invitation(request, token):
    if request.method == "GET":
        return render(request, "registration/signup.html", {"token": token, "form": InvitationSignupForm()})
    if request.method != "POST":
        return HttpResponse(status=405)
    form = InvitationSignupForm(request.POST)
    if not form.is_valid():
        return render(request, "registration/signup.html", {"token": token, "form": form}, status=400)

    email = form.cleaned_data["email"].strip().casefold()
    try:
        with transaction.atomic():
            invitation = Invitation.objects.select_for_update().select_related("household").get(token=token)
            if invitation.is_expired:
                return HttpResponseGone("This invitation has expired.")
            if invitation.email.casefold() != email:
                return HttpResponseBadRequest("This invitation was issued to a different email address.")
            user_model = get_user_model()
            if user_model.objects.filter(email__iexact=email).exists():
                return HttpResponseBadRequest("An account with this email already exists.")
            user = user_model.objects.create_user(username=email, email=email, password=form.cleaned_data["password1"])
            try:
                validate_password(form.cleaned_data["password1"], user)
            except ValidationError as exc:
                user.delete()
                form.add_error("password1", exc)
                return render(request, "registration/signup.html", {"token": token, "form": form}, status=400)
            Membership.objects.create(user=user, household=invitation.household, role=MembershipRole.MEMBER)
            invitation.delete()
    except Invitation.DoesNotExist:
        return HttpResponseNotFound("Invitation not found.")
    except IntegrityError:
        return HttpResponseBadRequest("The invitation could not be used.")
    return HttpResponse("Account created. Please sign in.", status=302, headers={"Location": "/accounts/login/"})


@require_POST
def create_invitation(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Authentication is required.")
    try:
        household = current_household(request)
        require_household_owner(request.user, household.pk)
    except PermissionDenied:
        return HttpResponseForbidden("Only a household owner may invite members.")
    form = forms.EmailField()
    try:
        email = form.clean(request.POST.get("email", "")).strip().casefold()
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    invitation = Invitation.objects.create(
        household=household,
        email=email,
        created_by=request.user,
        expires_at=timezone.now() + timezone.timedelta(days=7),
    )
    return HttpResponse(f"Invitation created: {invitation.token}", status=201)
