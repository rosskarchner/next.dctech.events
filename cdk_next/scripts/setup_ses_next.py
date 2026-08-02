#!/usr/bin/env python3
"""Idempotent SES setup for the next.dctech.events newsletter.

Mirrors dctech-newsletter/setup_ses.py's SESSetup class, but with fully
isolated -next resources: contact list `dctech-events-next-newsletter`
(topic `dctech-next`), template `dctech-events-next-newsletter`, and
configuration set `dctech-events-next` with an SNS event destination for
bounce/complaint feedback (isolated from production's identity-level
notifications).

Usage: setup_ses_next.py [--feedback-topic-arn arn:aws:sns:...]
       (omit the ARN to skip event-destination wiring)
"""
import argparse
import logging

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SES allows exactly ONE contact list per account. We use the long-standing
# list and topic so existing subscribers carry over; only the template and
# configuration set below belong to this stack.
CONTACT_LIST_NAME = 'newsletters'
TOPIC_NAME = 'dctech'
# Topic created for the parallel next.dctech.events build, retired at cutover.
RETIRED_TOPIC_NAME = 'dctech-next'
TEMPLATE_NAME = 'dctech-events-next-newsletter'
CONFIGURATION_SET_NAME = 'dctech-events-next'

TEMPLATE_CONTENT = {
    'Subject': 'DC Tech Events Weekly',
    'Html': '{{content}}<br><br>You received this email because you subscribed to DC Tech Events Weekly. '
            'To unsubscribe, click <a href="{{amazonSESUnsubscribeUrl}}">here</a>.',
    'Text': '{{content}}\n\nYou received this email because you subscribed to DC Tech Events Weekly. '
            'To unsubscribe, click here {{amazonSESUnsubscribeUrl}}',
}


class SESSetup:
    def __init__(self):
        self.ses_client = boto3.client('sesv2')

    def _exists(self, getter, **kwargs):
        try:
            getter(**kwargs)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'NotFoundException':
                return False
            raise

    def setup_contact_list(self):
        """Verify the subscription topic exists and drop the retired one.

        The topic itself predates this stack, so this never creates it —
        it only removes `dctech-next`, whose presence would otherwise show
        subscribers a phantom option on the SES preference page.
        """
        existing = self.ses_client.get_contact_list(ContactListName=CONTACT_LIST_NAME)
        topics = existing.get('Topics', [])
        names = [t['TopicName'] for t in topics]

        if TOPIC_NAME not in names:
            raise RuntimeError(
                f"Topic '{TOPIC_NAME}' not found on contact list "
                f"'{CONTACT_LIST_NAME}' (found: {names}). Refusing to create "
                f"it — subscribers are expected to already be on this topic.")
        logger.info(f"Topic '{TOPIC_NAME}' present on '{CONTACT_LIST_NAME}'.")

        if RETIRED_TOPIC_NAME in names:
            remaining = [t for t in topics if t['TopicName'] != RETIRED_TOPIC_NAME]
            logger.info(f"Removing retired topic '{RETIRED_TOPIC_NAME}'...")
            self.ses_client.update_contact_list(
                ContactListName=CONTACT_LIST_NAME,
                Description=existing.get('Description', ''),
                Topics=remaining)

    def setup_email_template(self):
        if self._exists(self.ses_client.get_email_template, TemplateName=TEMPLATE_NAME):
            logger.info(f"Email template '{TEMPLATE_NAME}' already exists. Updating...")
            self.ses_client.update_email_template(
                TemplateName=TEMPLATE_NAME, TemplateContent=TEMPLATE_CONTENT)
        else:
            logger.info(f"Creating email template '{TEMPLATE_NAME}'...")
            self.ses_client.create_email_template(
                TemplateName=TEMPLATE_NAME, TemplateContent=TEMPLATE_CONTENT)

    def setup_configuration_set(self):
        if self._exists(self.ses_client.get_configuration_set,
                        ConfigurationSetName=CONFIGURATION_SET_NAME):
            logger.info(f"Configuration set '{CONFIGURATION_SET_NAME}' already exists.")
            return
        logger.info(f"Creating configuration set '{CONFIGURATION_SET_NAME}'...")
        self.ses_client.create_configuration_set(
            ConfigurationSetName=CONFIGURATION_SET_NAME,
            SendingOptions={'SendingEnabled': True},
            ReputationOptions={'ReputationMetricsEnabled': True},
        )

    def setup_event_destination(self, topic_arn):
        name = 'feedback-sns'
        try:
            existing = self.ses_client.get_configuration_set_event_destinations(
                ConfigurationSetName=CONFIGURATION_SET_NAME)
            names = [d['Name'] for d in existing.get('EventDestinations', [])]
        except ClientError:
            names = []
        destination = {
            'Enabled': True,
            'MatchingEventTypes': ['BOUNCE', 'COMPLAINT'],
            'SnsDestination': {'TopicArn': topic_arn},
        }
        if name in names:
            logger.info('Updating SNS event destination...')
            self.ses_client.update_configuration_set_event_destination(
                ConfigurationSetName=CONFIGURATION_SET_NAME,
                EventDestinationName=name,
                EventDestination=destination)
        else:
            logger.info('Creating SNS event destination...')
            self.ses_client.create_configuration_set_event_destination(
                ConfigurationSetName=CONFIGURATION_SET_NAME,
                EventDestinationName=name,
                EventDestination=destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--feedback-topic-arn', default='')
    args = parser.parse_args()

    setup = SESSetup()
    setup.setup_contact_list()
    setup.setup_email_template()
    setup.setup_configuration_set()
    if args.feedback_topic_arn:
        setup.setup_event_destination(args.feedback_topic_arn)
    logger.info('SES setup complete.')


if __name__ == '__main__':
    main()
