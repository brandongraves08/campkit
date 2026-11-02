"""Dated manual/receipt price observations and household-scoped price history."""

import urllib.request
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import InventoryChange
from apps.enrichment.services import latest_price, price_history, record_price
from apps.inventory.models import InventoryVerification, PantryItem, PriceObservation

SOURCE = "https://receipts.example.test/2026/10/trail-mix"


def test_manual_price_observation_is_recorded_with_provenance(owner, household, pantry_item):
    observed_at = timezone.now() - timedelta(days=3)

    observation = record_price(
        item_id=pantry_item.pk,
        amount="4.99",
        currency="usd",
        merchant="Local Co-op",
        source_url=SOURCE,
        observed_at=observed_at,
        actor=owner,
    )

    assert isinstance(observation, PriceObservation)
    assert observation.item_id == pantry_item.pk
    assert observation.amount == Decimal("4.99")
    assert observation.currency == "USD"
    assert observation.merchant == "Local Co-op"
    assert observation.source == "manual"
    assert observation.source_url == SOURCE
    assert observation.observed_at == observed_at
    assert observation.actor == owner
    assert observation.confidence is None
    assert observation.verification_status == InventoryVerification.ESTIMATED
    assert observation.verified_by is None and observation.verified_at is None
    assert InventoryChange.objects.filter(
        household=household, action="price_observed", object_id=str(observation.pk)
    ).exists()


def test_receipt_source_is_retained(owner, pantry_item):
    observation = record_price(
        item_id=pantry_item.pk,
        amount=Decimal("12.50"),
        merchant="Big Box",
        source="receipt",
        actor=owner,
    )

    assert observation.source == "receipt"
    assert observation.merchant == "Big Box"


def test_observation_without_a_source_url_stays_unset(owner, pantry_item):
    observation = record_price(item_id=pantry_item.pk, amount="3.00", actor=owner)

    assert observation.source_url == ""


def test_price_requires_a_positive_amount(owner, pantry_item):
    for amount in ("0", "-1.00", "not a number", ""):
        with pytest.raises(ValidationError):
            record_price(item_id=pantry_item.pk, amount=amount, actor=owner)
    assert PriceObservation.objects.count() == 0


def test_price_rejects_sub_cent_precision_instead_of_rounding(owner, pantry_item):
    with pytest.raises(ValidationError) as excinfo:
        record_price(item_id=pantry_item.pk, amount="4.999", actor=owner)

    assert "amount" in excinfo.value.message_dict
    assert PriceObservation.objects.count() == 0


def test_price_rejects_out_of_range_amounts(owner, pantry_item):
    with pytest.raises(ValidationError):
        record_price(item_id=pantry_item.pk, amount="100000000.00", actor=owner)


def test_currency_must_be_a_three_letter_code(owner, pantry_item):
    with pytest.raises(ValidationError):
        record_price(item_id=pantry_item.pk, amount="1.00", currency="US", actor=owner)
    with pytest.raises(ValidationError):
        record_price(item_id=pantry_item.pk, amount="1.00", currency="USDX", actor=owner)


def test_merchant_and_source_url_are_bounded(owner, pantry_item):
    with pytest.raises(ValidationError):
        record_price(item_id=pantry_item.pk, amount="1.00", merchant="m" * 161, actor=owner)
    with pytest.raises(ValidationError):
        record_price(item_id=pantry_item.pk, amount="1.00", source_url="ftp://example.test/x", actor=owner)
    with pytest.raises(ValidationError):
        record_price(item_id=pantry_item.pk, amount="1.00", source_url="https://example.test/" + "a" * 200,
                     actor=owner)


def test_future_observations_are_refused(owner, pantry_item):
    with pytest.raises(ValidationError):
        record_price(item_id=pantry_item.pk, amount="1.00",
                     observed_at=timezone.now() + timedelta(days=1), actor=owner)


def test_naive_observation_times_are_refused(owner, pantry_item):
    with pytest.raises(ValidationError):
        record_price(item_id=pantry_item.pk, amount="1.00",
                     observed_at=timezone.make_naive(timezone.now()), actor=owner)


def test_iso_observation_strings_are_parsed(owner, pantry_item):
    observed_at = timezone.now() - timedelta(days=5)

    observation = record_price(item_id=pantry_item.pk, amount="2.00",
                               observed_at=observed_at.isoformat(), actor=owner)

    assert observation.observed_at == observed_at


def test_unauthenticated_actors_are_refused(pantry_item):
    with pytest.raises(PermissionDenied):
        record_price(item_id=pantry_item.pk, amount="1.00", actor=None)


def test_cross_household_price_writes_are_refused(other_owner, pantry_item):
    with pytest.raises(PermissionDenied):
        record_price(item_id=pantry_item.pk, amount="1.00", actor=other_owner)
    assert PriceObservation.objects.count() == 0


def test_unknown_item_is_reported_not_created(owner, pantry_item):
    with pytest.raises(PantryItem.DoesNotExist):
        record_price(item_id="00000000-0000-0000-0000-000000000000", amount="1.00", actor=owner)


def test_recording_a_price_never_fetches_the_source_url(owner, pantry_item, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("price recording must not fetch retailer pages")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    observation = record_price(item_id=pantry_item.pk, amount="9.99", source_url=SOURCE, actor=owner)

    assert observation.amount == Decimal("9.99")


def test_price_history_is_newest_first_and_scoped(owner, household, pantry_item, make_candidate):
    now = timezone.now()
    record_price(item_id=pantry_item.pk, amount="1.00", observed_at=now - timedelta(days=10), actor=owner)
    record_price(item_id=pantry_item.pk, amount="2.00", observed_at=now - timedelta(days=1), actor=owner)

    history = price_history(actor=owner, household=household, item_id=pantry_item.pk)

    assert [row["amount"] for row in history] == ["2.00", "1.00"]
    assert all("observed_at" in row and "source_url" in row for row in history)


def test_latest_price_flags_stale_observations(owner, household, pantry_item):
    now = timezone.now()
    record_price(item_id=pantry_item.pk, amount="1.00", observed_at=now - timedelta(days=90), actor=owner)
    stale = latest_price(actor=owner, household=household, item_id=pantry_item.pk)
    assert stale["is_stale"] is True
    assert stale["amount"] == "1.00"

    record_price(item_id=pantry_item.pk, amount="2.00", observed_at=now - timedelta(days=1), actor=owner)
    fresh = latest_price(actor=owner, household=household, item_id=pantry_item.pk)
    assert fresh["is_stale"] is False
    assert fresh["amount"] == "2.00"


def test_no_price_history_returns_nothing_rather_than_a_guess(owner, household, pantry_item):
    assert price_history(actor=owner, household=household, item_id=pantry_item.pk) == []
    assert latest_price(actor=owner, household=household, item_id=pantry_item.pk) is None


def test_price_history_is_household_scoped(other_owner, other_household, owner, household, pantry_item):
    record_price(item_id=pantry_item.pk, amount="2.00", actor=owner)

    with pytest.raises(PermissionDenied):
        price_history(actor=other_owner, household=other_household, item_id=pantry_item.pk)
    with pytest.raises(PermissionDenied):
        latest_price(actor=other_owner, household=other_household, item_id=pantry_item.pk)
