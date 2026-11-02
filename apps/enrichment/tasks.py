"""Enrichment job entry points.

No broker is installed in this milestone, so enrichment runs as plain,
idempotent callables that a future Celery/task runner can wrap. An actor is
always required: this app never invents a system identity to write provenance.
"""


def _resolve_item(item_id, actor):
    from apps.inventory.services import get_pantry_item

    return get_pantry_item(actor=actor, item_id=item_id)


def enrich_pantry_item(item_id, *, actor, refresh=False, query=""):
    """Look up nutrition for one pantry item and store what the provider supplied."""
    from apps.enrichment.services import enrich_nutrition

    item = _resolve_item(item_id, actor)
    return enrich_nutrition(
        actor=actor, household=item.household, item_id=item.pk, query=query, refresh=refresh
    )


def research_pantry_item(item_id, *, actor):
    """Store dated research citations for one pantry item."""
    from apps.enrichment.services import research_product

    item = _resolve_item(item_id, actor)
    return research_product(actor=actor, household=item.household, item_id=item.pk)
