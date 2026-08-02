"""Weekly newsletter sender — cron(0 11 ? * MON *), same as production.

Port of dctech-newsletter's send_newsletter_to_subscribers(), with the
content source modernized: rendered directly from DynamoDB via calgen
(render.py) instead of HTTP-scraping the live site. Sends through this
stack's isolated SES template/contact list/configuration set.
"""
import json
import os

import boto3

from render import render_newsletter

ses = boto3.client('sesv2')

FROM_EMAIL = os.environ.get('FROM_EMAIL', 'outbound@dctech.events')
REPLY_TO_EMAIL = os.environ.get('REPLY_TO_EMAIL', 'ross@karchner.com')
CONTACT_LIST_NAME = os.environ.get('CONTACT_LIST_NAME', 'newsletters')
TOPIC_NAME = os.environ.get('TOPIC_NAME', 'dctech')
TEMPLATE_NAME = os.environ.get('TEMPLATE_NAME', 'dctech-events-next-newsletter')
CONFIGURATION_SET = os.environ.get('CONFIGURATION_SET', 'dctech-events-next')


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


def send_newsletter_to_subscribers(dry_run=False, only_addresses=None):
    """Send the newsletter to all confirmed subscribers. Returns a summary."""
    try:
        html_content, text_content = render_newsletter()
    except Exception as e:
        error_msg = f'Failed to render newsletter content: {e}'
        print(error_msg)
        return {'status': 'error', 'reason': error_msg}

    try:
        contacts = _list_all_contacts()
        success_count = 0
        error_count = 0

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
            if dry_run:
                print(f"[dry-run] would send to {contact['EmailAddress']}")
                success_count += 1
                continue

            try:
                ses.send_email(
                    FromEmailAddress=FROM_EMAIL,
                    ReplyToAddresses=[REPLY_TO_EMAIL],
                    Destination={'ToAddresses': [contact['EmailAddress']]},
                    Content={
                        'Template': {
                            'TemplateName': TEMPLATE_NAME,
                            'TemplateData': json.dumps({'content': html_content}),
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
                print(f"Error sending to {contact['EmailAddress']}: {e}")
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
