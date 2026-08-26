"""Tests for the /updates social cross-poster.

Run: python -m pytest test_app.py
"""
import os
from decimal import Decimal

import pytest

os.environ.setdefault("DYNAMODB_TABLE_NAME", "test-table")
os.environ.setdefault("MASTODON_SECRET_NAME", "test/mastodon")
os.environ.setdefault("BLUESKY_SECRET_NAME", "test/bluesky")
os.environ.setdefault("SITE_BASE_URL", "https://dctech.events")

import app  # noqa: E402
import networks  # noqa: E402


def _weekly(**overrides):
    item = {
        "PK": "UPDATE#2026-W33",
        "SK": "META",
        "week_start": "2026-08-10",
        "week_end": "2026-08-16",
        "title": "DC Tech Events for the week of August 10, 2026",
        "event_count": Decimal("27"),
        "events": [{"title": "Python DC"}, {"title": "DC Ruby"},
                   {"title": "Hack and Tell"}],
    }
    item.update(overrides)
    return item


def _post(**overrides):
    item = {
        "PK": "POST#new-newsletter",
        "SK": "META",
        "slug": "new-newsletter",
        "title": "The newsletter is back",
        "status": "published",
        "body": "We're **restarting** the [newsletter](https://example.com).",
    }
    item.update(overrides)
    return item


# ── URL and content derivation ───────────────────────────────────────


def test_weekly_url_is_unpadded_to_match_flask_int_converter():
    # calgen builds /updates/2026/8/10/ via Flask's <int:> converter; a
    # zero-padded path is one the site never serves.
    assert app._describe(_weekly())["url"] == \
        "https://dctech.events/updates/2026/8/10/"


def test_weekly_summary_matches_calgen_phrasing():
    summary = app._describe(_weekly())["summary"]
    assert summary == ("27 events this week: Python DC, DC Ruby, "
                       "Hack and Tell, and 24 more.")


def test_added_roundup_is_filed_under_its_publication_date():
    # From 2026-08-12 the key is the publish date and the post lists what was
    # added that week; week_start is kept only so older readers agree.
    item = _weekly(PK="UPDATE#2026-08-12", published_on="2026-08-12",
                   week_start="2026-08-12")
    assert app._describe(item)["url"] == \
        "https://dctech.events/updates/2026/8/12/"


def test_added_roundup_uses_its_stored_summary():
    item = _weekly(published_on="2026-08-12",
                   summary="23 events added between August 5–11: A, B, C.")
    assert app._describe(item)["summary"] == \
        "23 events added between August 5–11: A, B, C."


def test_weekly_summary_without_events():
    item = _weekly(events=[], event_count=Decimal("0"))
    assert app._describe(item)["summary"] == "No events were listed for this week."


def test_weekly_summary_singular_event():
    item = _weekly(events=[{"title": "Only One"}], event_count=Decimal("1"))
    assert app._describe(item)["summary"] == "1 event this week: Only One."


def test_weekly_title_falls_back_to_the_formatted_week():
    item = _weekly()
    del item["title"]
    assert app._describe(item)["title"] == \
        "DC Tech Events for the week of August 10, 2026"


def test_free_post_url_uses_its_slug():
    assert app._describe(_post())["url"] == \
        "https://dctech.events/updates/new-newsletter/"


def test_free_post_summary_strips_markdown_from_the_body():
    assert app._describe(_post())["summary"] == \
        "We're restarting the newsletter."


def test_free_post_prefers_an_explicit_summary():
    item = _post(summary="Sign up for the weekly mail.")
    assert app._describe(item)["summary"] == "Sign up for the weekly mail."


@pytest.mark.parametrize("status", ["draft", "Draft", None])
def test_draft_posts_are_never_announced(status):
    item = _post()
    if status is None:
        del item["status"]
    else:
        item["status"] = status
    assert app._describe(item) is None


@pytest.mark.parametrize("pk", ["EVENT#abc", "GROUP#python-dc",
                                "SOCIAL#UPDATE#2026-W33"])
def test_unrelated_entities_are_not_postable(pk):
    assert app._describe({"PK": pk, "SK": "META"}) is None


def test_weekly_without_a_week_start_is_skipped():
    item = _weekly()
    del item["week_start"]
    assert app._describe(item) is None


# ── composition ──────────────────────────────────────────────────────


def test_compose_puts_title_summary_and_link_on_their_own_lines():
    text = app.compose(app._describe(_weekly()), 500)
    assert text == (
        "DC Tech Events for the week of August 10, 2026\n\n"
        "27 events this week: Python DC, DC Ruby, Hack and Tell, and 24 more.\n\n"
        "https://dctech.events/updates/2026/8/10/"
    )


def test_compose_fits_the_bluesky_limit():
    post = app._describe(_weekly(events=[
        {"title": "A very long meetup name that goes on and on"} for _ in range(30)
    ], event_count=Decimal("30")))
    text = app.compose(post, networks.BLUESKY_CHAR_LIMIT)
    assert len(text) <= networks.BLUESKY_CHAR_LIMIT
    assert text.endswith("https://dctech.events/updates/2026/8/10/")


def test_compose_drops_the_summary_before_it_drops_the_link():
    post = {"title": "T" * 240, "summary": "S" * 200,
            "url": "https://dctech.events/updates/2026/8/10/"}
    text = app.compose(post, networks.BLUESKY_CHAR_LIMIT)
    assert len(text) <= networks.BLUESKY_CHAR_LIMIT
    assert text.endswith(post["url"])
    assert "S" not in text


def test_compose_truncates_an_over_long_title_rather_than_the_url():
    url = "https://dctech.events/updates/2026/8/10/"
    text = app.compose({"title": "word " * 200, "summary": "", "url": url}, 100)
    assert len(text) <= 100
    assert text.endswith(url)


def test_clip_breaks_on_a_word_boundary():
    assert app._clip("one two three four", 12) == "one two…"


def test_clip_leaves_short_text_alone():
    assert app._clip("short", 40) == "short"


# ── stream filtering ─────────────────────────────────────────────────


def _record(pk, *, sk="META", name="INSERT", image=True):
    record = {"eventName": name,
              "dynamodb": {"Keys": {"PK": {"S": pk}, "SK": {"S": sk}}}}
    if image:
        record["dynamodb"]["NewImage"] = {"PK": {"S": pk}, "SK": {"S": sk},
                                          "title": {"S": "T"}}
    return record


def test_stream_selects_only_updates_and_posts():
    records = [_record("UPDATE#2026-W33"), _record("POST#hello"),
               _record("EVENT#abc"), _record("GROUP#python-dc")]
    got = [i["PK"] for i in app._items_from_stream(records)]
    assert got == ["UPDATE#2026-W33", "POST#hello"]


def test_stream_ignores_the_dedupe_record_it_writes_itself():
    # Otherwise every post would loop the publisher back through its own write.
    assert app._items_from_stream([_record("SOCIAL#UPDATE#2026-W33")]) == []


def test_stream_ignores_removals():
    records = [_record("POST#hello", name="REMOVE", image=False)]
    assert app._items_from_stream(records) == []


def test_stream_covers_the_draft_to_published_edit():
    # /edit saves a draft first, so the announcement rides a MODIFY.
    records = [_record("POST#hello", name="MODIFY")]
    assert [i["PK"] for i in app._items_from_stream(records)] == ["POST#hello"]


def test_stream_ignores_non_meta_rows():
    assert app._items_from_stream([_record("POST#hello", sk="SOCIAL")]) == []


def test_stream_deserializes_the_new_image():
    record = {
        "eventName": "INSERT",
        "dynamodb": {
            "Keys": {"PK": {"S": "UPDATE#2026-W33"}, "SK": {"S": "META"}},
            "NewImage": {
                "PK": {"S": "UPDATE#2026-W33"}, "SK": {"S": "META"},
                "week_start": {"S": "2026-08-10"},
                "event_count": {"N": "27"},
                "events": {"L": [{"M": {"title": {"S": "Python DC"}}}]},
            },
        },
    }
    item = app._items_from_stream([record])[0]
    assert item["event_count"] == Decimal("27")
    assert item["events"][0]["title"] == "Python DC"
    assert app._describe(item)["url"] == \
        "https://dctech.events/updates/2026/8/10/"


# ── bluesky facets ───────────────────────────────────────────────────


def test_link_facet_offsets_are_byte_based():
    # The em dash is 3 UTF-8 bytes, so a character-based offset would point
    # two bytes short and Bluesky would render a truncated link.
    url = "https://dctech.events/updates/2026/8/10/"
    text = f"Week — {url}"
    facet = networks.link_facets(text)[0]
    assert facet["index"]["byteStart"] == len("Week — ".encode("utf-8"))
    assert facet["index"]["byteEnd"] == \
        facet["index"]["byteStart"] + len(url.encode("utf-8"))
    assert facet["features"][0]["uri"] == url


def test_link_facet_covers_the_composed_post():
    post = app._describe(_weekly())
    text = app.compose(post, networks.BLUESKY_CHAR_LIMIT)
    facets = networks.link_facets(text)
    assert len(facets) == 1
    raw = text.encode("utf-8")
    start, end = facets[0]["index"]["byteStart"], facets[0]["index"]["byteEnd"]
    assert raw[start:end].decode("utf-8") == post["url"]


def test_no_facets_when_there_is_no_link():
    assert networks.link_facets("just some words") == []


# ── the Monday link post ─────────────────────────────────────────────


def _link_post(**overrides):
    item = {
        "PK": "UPDATE#2026-08-31",
        "SK": "META",
        "post_kind": "link",
        "week_id": "2026-W36",
        "published_on": "2026-08-31",
        "week_start": "2026-08-31",
        "link_url": "/week/2026-W36/",
        "title": "DC Tech Events for the week of August 31, 2026",
        "summary": "23 events on the calendar for August 31–September 6: A, B, C.",
        "event_count": Decimal("23"),
    }
    item.update(overrides)
    return item


def test_link_post_syndicates_its_target_not_its_permalink():
    # A link post is its link; sending readers via /updates/ would be two hops
    # to the listing the post exists to point at.
    assert app._describe(_link_post())["url"] == \
        "https://dctech.events/week/2026-W36/"


def test_link_post_keeps_its_title_and_stored_summary():
    post = app._describe(_link_post())
    assert post["title"] == "DC Tech Events for the week of August 31, 2026"
    assert post["summary"].startswith("23 events on the calendar")


def test_link_post_is_still_keyed_on_its_own_pk_for_dedupe():
    # The SOCIAL# record follows the post, not the target, so re-pointing a
    # link post cannot cause a repost.
    assert app._describe(_link_post())["pk"] == "UPDATE#2026-08-31"


def test_link_post_with_no_target_falls_back_to_its_week_page():
    # Never the /updates/ path: it is not built for a link post, so
    # announcing it would be announcing a 404.
    assert app._describe(_link_post(link_url=""))["url"] == \
        "https://dctech.events/week/2026-W36/"


def test_link_post_with_nothing_to_point_at_is_not_announced():
    assert app._describe(_link_post(link_url="", week_id="")) is None


def test_link_post_accepts_an_absolute_target():
    item = _link_post(link_url="https://example.com/elsewhere/")
    assert app._describe(item)["url"] == "https://example.com/elsewhere/"


def test_a_roundup_ignores_a_stray_link_url():
    # Only post_kind switches the behaviour, so a roundup that happens to
    # carry link_url still syndicates its own permalink.
    item = _weekly(link_url="/week/2026-W33/")
    assert app._describe(item)["url"] == \
        "https://dctech.events/updates/2026/8/10/"


def test_link_post_composes_title_summary_and_target():
    text = app.compose(app._describe(_link_post()), app.MASTODON_CHAR_LIMIT)
    assert text.splitlines()[0] == \
        "DC Tech Events for the week of August 31, 2026"
    assert text.endswith("https://dctech.events/week/2026-W36/")
