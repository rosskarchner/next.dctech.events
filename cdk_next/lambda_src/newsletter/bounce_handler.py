"""SES bounce/complaint handler (SNS-subscribed).

Port of dctech-newsletter's handle_ses_notification. Handles both classic
notification format (notificationType/bounce.bouncedRecipients) and the
SESv2 configuration-set event format (eventType), since this stack wires
feedback through a configuration-set event destination for isolation from
production's identity-level notifications.
"""
import json
import os

import boto3

ses = boto3.client('sesv2')

CONTACT_LIST_NAME = os.environ.get('CONTACT_LIST_NAME', 'newsletters')


def _delete_contact(email):
    try:
        ses.delete_contact(ContactListName=CONTACT_LIST_NAME, EmailAddress=email)
        print(f'Removed {email} from {CONTACT_LIST_NAME}')
    except Exception as e:
        print(f'Error removing contact {email}: {e}')


def _handle_message(message):
    notification_type = message.get('notificationType') or message.get('eventType')

    if notification_type == 'Bounce':
        bounce = message.get('bounce', {})
        if bounce.get('bounceType') == 'Permanent':
            for recipient in bounce.get('bouncedRecipients', []):
                _delete_contact(recipient['emailAddress'])
    elif notification_type == 'Complaint':
        complaint = message.get('complaint', {})
        for recipient in complaint.get('complainedRecipients', []):
            _delete_contact(recipient['emailAddress'])


def lambda_handler(event, context):
    for record in event.get('Records', []):
        try:
            message = json.loads(record['Sns']['Message'])
            _handle_message(message)
        except Exception as e:
            print(f'Error processing record: {e}')
    return {'status': 'processed'}
