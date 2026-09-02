"""Weekly newsletter sender — cron(0 11 ? * MON *), same as production.

Port of dctech-newsletter's send_newsletter_to_subscribers(), with the
content source modernized: rendered directly from DynamoDB via calgen
(render.py) instead of HTTP-scraping the live site. Sends through this
stack's isolated SES template/contact list/configuration set.

Per-subscriber personalization (next_dctech_events preferences feature):
the site is built once (render.build_site — the expensive DynamoDB
export/pipeline step), subscribers are grouped by their exact
(categories, regions) preference signature, and render.render_for_filters
is called once per distinct group rather than once per subscriber — a
subscriber with no preferences set (by far the common case, and every
subscriber that predates this feature) shares the single unfiltered
render with every other unfiltered subscriber. Only the manage-preferences
link substituted into each group's rendered content is truly per-subscriber,
done with a cheap string .replace() rather than a re-render.
"""
import json
import os

import boto3

import db
import magic_link
from constants import FROM_EMAIL, REPLY_TO_EMAIL, CONTACT_LIST_NAME
from render import build_site, render_for_filters, render_newsletter

ses = boto3.client('sesv2')

TOPIC_NAME = os.environ.get('TOPIC_NAME', 'dctech')
TEMPLATE_NAME = os.environ.get('TEMPLATE_NAME', 'dctech-events-next-newsletter')
CONFIGURATION_SET = os.environ.get('CONFIGURATION_SET', 'dctech-events-next')
PREFERENCES_PATH = '/edit/preferences.html'


def _list_all_contacts():
    contacts = []
    kwargs = {'ContactListName': CONTACT_LIST_NAME}
    while True:
        response = ses.list_contacts(**kwargs)
        contacts.extend(response.get('Contacts', []))
        token = response.get('NextToken')
        if not token:
            return contacts
        kwargs['NextToken'] = token


def _preferences_link(email):
    timestamp, signature = magic_link.generate_token(email, purpose='prefs')
    return magic_link.build_link(email, timestamp, signature, path=PREFERENCES_PATH)


def _group_subscribers(emails):
    """email -> that subscriber's (categories, regions) preference
    signature (tuples, so they're hashable group keys). No preferences row
    — the common case, and every subscriber who predates this feature —
    groups under the empty/unfiltered signature, same as an explicit
    "everything" selection would."""
    groups = {}
    for email in emails:
        prefs = db.get_subscriber_preferences(email) or {}
        key = (tuple(prefs.get('categories') or []), tuple(prefs.get('regions') or []))
        groups.setdefault(key, []).append(email)
    return groups


def send_newsletter_to_subscribers(dry_run=False, only_addresses=None):
    """Send the newsletter to all confirmed subscribers, each personalized
    to their own category/region preferences (and a unique manage-preferences
    link). Returns a summary."""
    try:
        build_site()
    except Exception as e:
        error_msg = f'Failed to build newsletter site: {e}'
        print(error_msg)
        return {'status': 'error', 'reason': error_msg}

    # Imported only after build_site() — that's what actually sets up the
    # site dir and resets calgen's config cache (calgen.routes.common does
    # config = get_config() at import time), matching render.py's own
    # lazy-import discipline for every other calgen internal.
    from calgen.routes.newsletter import PREFERENCES_LINK_PLACEHOLDER

    try:
        contacts = _list_all_contacts()
        subscribed_emails = []
        for contact in contacts:
            topic_preferences = contact.get('TopicPreferences', [])
            is_subscribed = any(
                pref['TopicName'] == TOPIC_NAME and pref['SubscriptionStatus'] == 'OPT_IN'
                for pref in topic_preferences
            )
            if not is_subscribed:
                continue
            if only_addresses and contact['EmailAddress'] not in only_addresses:
                continue
            subscribed_emails.append(contact['EmailAddress'])

        success_count = 0
        error_count = 0

        for (categories, regions), emails in _group_subscribers(subscribed_emails).items():
            try:
                html_content, _text_content = render_for_filters(
                    list(categories), list(regions))
            except Exception as e:
                print(f"Error rendering group categories={categories} "
                      f"regions={regions}: {e}")
                error_count += len(emails)
                continue

            for email in emails:
                if dry_run:
                    print(f"[dry-run] would send to {email} "
                          f"(categories={categories}, regions={regions})")
                    success_count += 1
                    continue

                try:
                    personalized_html = html_content.replace(
                        PREFERENCES_LINK_PLACEHOLDER, _preferences_link(email))
                    ses.send_email(
                        FromEmailAddress=FROM_EMAIL,
                        ReplyToAddresses=[REPLY_TO_EMAIL],
                        Destination={'ToAddresses': [email]},
                        Content={
                            'Template': {
                                'TemplateName': TEMPLATE_NAME,
                                'TemplateData': json.dumps({'content': personalized_html}),
                            }
                        },
                        ListManagementOptions={
                            'ContactListName': CONTACT_LIST_NAME,
                            'TopicName': TOPIC_NAME,
                        },
                        ConfigurationSetName=CONFIGURATION_SET,
                    )
                    success_count += 1
                except Exception as e:
                    print(f"Error sending to {email}: {e}")
                    error_count += 1

        return {
            'status': 'completed',
            'successful_sends': success_count,
            'failed_sends': error_count,
            'message': f'Newsletter sent successfully to {success_count} subscribers ({error_count} failures)',
        }
    except Exception as e:
        print(f"Error sending newsletter: {e}")
        return {'status': 'error', 'reason': str(e)}


def lambda_handler(event, context):
    event = event or {}
    if event.get('render_only'):
        html_content, text_content = render_newsletter()
        return {'status': 'rendered',
                'html': html_content, 'text': text_content}
    return send_newsletter_to_subscribers(
        dry_run=bool(event.get('dry_run')),
        only_addresses=event.get('only_addresses'),
    )
